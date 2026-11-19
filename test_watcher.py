import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import watcher


def catalogue(stock=0):
    return {'code': 0, 'data': {'enabled': True, 'tiers': [
        {'tier': 'claude', 'cities': [
            {'country': 'DE', 'city': 'Frankfurt am Main', 'available': stock},
            {'country': 'US', 'city': 'New York', 'available': 100},
            {'country': 'GB', 'city': 'London', 'available': 0}]}]}}


def chunk_source(literal, ident='blockMap'):
    """Minified noise around the site block, with identifiers other than sA/g1."""
    return (
        '/*vendor*/function q(v){return v+1}const z={ok:1};'
        f'const {ident}={literal},apply=(tier,rows)=>rows.map('
        f'row=>{{var hit;return(hit={ident}[tier])!=null&&hit.includes(row.country)'
        '?{...row,available:0}:row});'
        '/*end*/export{q};'
    )


class FakeSite:
    """URL → HTML / index bundle / main chunks. Records every fetch."""

    def __init__(self, literal='{claude:[]}', index='index-a.js', main='main-real.js'):
        self.literal = literal
        self.index = index
        self.main = main
        self.main_body = None
        self.fetched = []

    def fetch_text(self, url):
        self.fetched.append(url)
        try:
            return self.pages()[url]
        except KeyError:
            raise RuntimeError('Could not read site restock overrides') from None

    def pages(self):
        body = self.main_body if self.main_body is not None else chunk_source(self.literal)
        html = (f'<html><script type="module" crossorigin src="/assets/{self.index}">'
                f'</script></html>')
        index_js = f'const assets=["assets/main-decoy.js","assets/{self.main}"];'
        return {
            watcher.ORDER_URL: html,
            f'https://proxy302.com/assets/{self.index}': index_js,
            'https://proxy302.com/assets/main-decoy.js': 'var unrelated={a:1};function noop(x,y){return x}',
            f'https://proxy302.com/assets/{self.main}': body,
        }

    def mains(self):
        return [url for url in self.fetched if '/assets/main-' in url]


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name)
        self.patch = patch.object(watcher, 'CONFIG', self.config)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.site = FakeSite()

    def check(self, **kwargs):
        kwargs.setdefault('fetch_text', self.site.fetch_text)
        return watcher.check(**kwargs)

    def test_positive_stock_only(self):
        self.assertEqual(watcher.german_stock(catalogue()), [])
        self.assertEqual(watcher.german_stock(catalogue(2)), [('Frankfurt am Main', 2)])

    def test_only_claude(self):
        payload = catalogue()
        payload['data']['tiers'].append({'tier': 'gemini', 'cities': [
            {'country': 'DE', 'city': 'Frankfurt', 'available': 50}]})
        self.assertEqual(watcher.german_stock(payload), [])

    def test_notify_once_and_rearm(self):
        sent = []
        for stock in [0, 2, 3, 0, 1]:
            self.check(fetch=lambda: catalogue(stock), send=lambda text, silent=False: sent.append(text) if not silent else None)
        self.assertEqual(len(sent), 2)
        self.assertIn('Frankfurt am Main: 2', sent[0])
        self.assertTrue(json.loads((self.config / 'state.json').read_text())['available'])

    def test_initial_stock_notifies(self):
        sent = []
        self.check(fetch=lambda: catalogue(1), send=lambda text, silent=False: sent.append(text) if not silent else None)
        self.assertEqual(len(sent), 1)

    def test_delivery_failure_retries(self):
        def fail(*_, **__):
            raise RuntimeError('delivery failed')
        with self.assertRaises(RuntimeError):
            self.check(fetch=lambda: catalogue(1), send=fail)
        self.assertFalse((self.config / 'state.json').exists())
        sent = []
        self.check(fetch=lambda: catalogue(1), send=lambda text, silent=False: sent.append(text) if not silent else None)
        self.assertEqual(len(sent), 1)

    def test_source_failure_preserves_state(self):
        self.check(fetch=lambda: catalogue(), send=lambda *a, **k: None)
        before = (self.config / 'state.json').read_text()
        for payload in [{'code': -1}, {'code': 0, 'data': {'enabled': False}}]:
            with self.assertRaises(RuntimeError):
                self.check(fetch=lambda: payload, send=lambda *a, **k: None)
        self.assertEqual(before, (self.config / 'state.json').read_text())

    def test_invalid_count_rejected(self):
        for stock in [-1, '3', True]:
            with self.assertRaises(RuntimeError):
                watcher.german_stock(catalogue(stock))

    def test_digest_every_six_hours(self):
        import datetime
        t0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        digests = []
        send = lambda text, silent=False: digests.append(text) if silent else None
        for hours in [0, 1, 5.9, 6, 11, 12]:
            self.check(fetch=lambda: catalogue(), send=send,
                       now=t0 + datetime.timedelta(hours=hours))
        self.assertEqual(len(digests), 3)
        self.assertIn('США: 100 IP\n   • New York: 100', digests[0])
        self.assertIn('Германия: нет', digests[0])
        self.assertNotIn('Restocking', digests[0])

    def test_digest_failure_keeps_alert_state(self):
        def send(text, silent=False):
            if silent:
                raise RuntimeError('delivery failed')
        with self.assertRaises(RuntimeError):
            self.check(fetch=lambda: catalogue(1), send=send)
        self.assertTrue(json.loads((self.config / 'state.json').read_text())['available'])

    def test_parser_extracts_blocks(self):
        site = FakeSite(literal='{claude:["DE"]}')
        self.assertEqual(watcher.site_overrides(site.fetch_text), {'claude': {'DE'}})
        quoted = FakeSite(literal='{claude:["DE"],"gpt-grok":[]}')
        self.assertEqual(watcher.site_overrides(quoted.fetch_text),
                         {'claude': {'DE'}, 'gpt-grok': set()})
        live = ('prefix();const sA={claude:["DE"]},g1=(e,n)=>n.map(s=>{var a;return(a=sA[e])'
                '!=null&&a.includes(s.country)?{...s,available:0}:s})suffix();')
        shaped = FakeSite()
        shaped.main_body = live
        self.assertEqual(watcher.site_overrides(shaped.fetch_text), {'claude': {'DE'}})

    def test_blocked_germany_matches_site(self):
        self.site.literal = '{claude:["DE"]}'
        alerts, digests = [], []

        def send(text, silent=False):
            (digests if silent else alerts).append(text)

        self.check(fetch=lambda: catalogue(739), send=send)
        self.assertEqual(alerts, [])
        self.assertIn('Германия: нет — на сайте «Restocking» (в API: 739)', digests[0])
        self.assertIn('Frankfurt am Main: нет', digests[0])
        self.assertIn('США: 100 IP\n   • New York: 100', digests[0])
        self.assertFalse(json.loads((self.config / 'state.json').read_text())['available'])

    def test_empty_block_lets_alert_fire(self):
        self.site.literal = '{claude:[]}'
        sent = []
        self.check(fetch=lambda: catalogue(4),
                   send=lambda text, silent=False: sent.append(text) if not silent else None)
        self.assertEqual(len(sent), 1)
        self.assertIn('Frankfurt am Main: 4', sent[0])

    def test_override_cache_skips_chunks_until_bundle_changes(self):
        self.site.literal = '{claude:["DE"]}'
        alerts = []
        send = lambda text, silent=False: alerts.append(text) if not silent else None
        self.check(fetch=lambda: catalogue(739), send=send)
        self.assertEqual(alerts, [])
        self.assertIn('https://proxy302.com/assets/main-decoy.js', self.site.fetched)
        self.assertIn('https://proxy302.com/assets/main-real.js', self.site.fetched)
        self.site.fetched.clear()
        self.check(fetch=lambda: catalogue(739), send=send)
        self.assertEqual(self.site.mains(), [])
        self.assertNotIn('https://proxy302.com/assets/index-a.js', self.site.fetched)
        self.assertIn(watcher.ORDER_URL, self.site.fetched)
        self.assertEqual(alerts, [])
        self.site.fetched.clear()
        self.site.index = 'index-b.js'
        self.site.main = 'main-next.js'
        self.site.literal = '{claude:[]}'
        self.check(fetch=lambda: catalogue(739), send=send)
        self.assertIn('https://proxy302.com/assets/main-next.js', self.site.fetched)
        self.assertEqual(len(alerts), 1)
        self.assertIn('Frankfurt am Main: 739', alerts[0])

    def test_override_failure_uses_cache_or_keeps_state(self):
        import datetime
        self.site.literal = '{claude:["DE"]}'
        alerts, digests = [], []

        def send(text, silent=False):
            (digests if silent else alerts).append(text)

        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        self.check(fetch=lambda: catalogue(739), send=send, now=start)
        self.assertEqual(alerts, [])
        self.assertIn('в API: 739', digests[0])

        def boom(url):
            raise RuntimeError('HTTP error 503')

        digests.clear()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.check(fetch=lambda: catalogue(739), send=send, fetch_text=boom,
                       now=start + datetime.timedelta(hours=6))
        self.assertIn('Warning: site overrides unavailable, using cached', buf.getvalue())
        self.assertEqual(alerts, [])
        self.assertIn('Германия: нет — на сайте «Restocking» (в API: 739)', digests[0])

        bare = self.config / 'state.json'
        bare.write_text('{"available": false, "cities": []}\n')
        before = bare.read_text()
        with self.assertRaises(RuntimeError):
            self.check(fetch=lambda: catalogue(5), send=send, fetch_text=boom)
        self.assertEqual(before, bare.read_text())

        bare.unlink()
        with self.assertRaises(RuntimeError):
            self.check(fetch=lambda: catalogue(5), send=send, fetch_text=boom)
        self.assertFalse(bare.exists())

    def test_unrecognisable_chunk(self):
        self.site.main_body = 'var totally = unrelated;'
        with self.assertRaises(RuntimeError) as caught:
            watcher.site_overrides(self.site.fetch_text)
        self.assertEqual(str(caught.exception), 'Could not read site restock overrides')
        with self.assertRaises(RuntimeError) as caught:
            self.check(fetch=lambda: catalogue(3), send=lambda *a, **k: None)
        self.assertEqual(str(caught.exception), 'Could not read site restock overrides')
        self.assertFalse((self.config / 'state.json').exists())


if __name__ == '__main__':
    unittest.main()
