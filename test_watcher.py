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


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name)
        self.patch = patch.object(watcher, 'CONFIG', self.config)
        self.patch.start()
        self.addCleanup(self.patch.stop)

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
            watcher.check(fetch=lambda: catalogue(stock), send=lambda text, silent=False: sent.append(text) if not silent else None)
        self.assertEqual(len(sent), 2)
        self.assertIn('Frankfurt am Main: 2', sent[0])
        self.assertTrue(json.loads((self.config / 'state.json').read_text())['available'])

    def test_initial_stock_notifies(self):
        sent = []
        watcher.check(fetch=lambda: catalogue(1), send=lambda text, silent=False: sent.append(text) if not silent else None)
        self.assertEqual(len(sent), 1)

    def test_delivery_failure_retries(self):
        def fail(*_, **__):
            raise RuntimeError('delivery failed')
        with self.assertRaises(RuntimeError):
            watcher.check(fetch=lambda: catalogue(1), send=fail)
        self.assertFalse((self.config / 'state.json').exists())
        sent = []
        watcher.check(fetch=lambda: catalogue(1), send=lambda text, silent=False: sent.append(text) if not silent else None)
        self.assertEqual(len(sent), 1)

    def test_source_failure_preserves_state(self):
        watcher.check(fetch=lambda: catalogue(), send=lambda *a, **k: None)
        before = (self.config / 'state.json').read_text()
        for payload in [{'code': -1}, {'code': 0, 'data': {'enabled': False}}]:
            with self.assertRaises(RuntimeError):
                watcher.check(fetch=lambda: payload, send=lambda *a, **k: None)
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
            watcher.check(fetch=lambda: catalogue(), send=send,
                          now=t0 + datetime.timedelta(hours=hours))
        self.assertEqual(len(digests), 3)
        self.assertIn('США: 100 IP\n   • New York: 100', digests[0])
        self.assertIn('Германия: нет', digests[0])

    def test_digest_failure_keeps_alert_state(self):
        def send(text, silent=False):
            if silent:
                raise RuntimeError('delivery failed')
        with self.assertRaises(RuntimeError):
            watcher.check(fetch=lambda: catalogue(1), send=send)
        self.assertTrue(json.loads((self.config / 'state.json').read_text())['available'])


if __name__ == '__main__':
    unittest.main()
