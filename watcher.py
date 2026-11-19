#!/usr/bin/env python3
"""Watch public Proxy302 AI ISP (Claude) inventory and notify via Telegram; never purchases anything."""
import argparse
import datetime
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

CONFIG = Path(os.environ.get('PROXY302_WATCHER_DIR') or
              Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'proxy302-watcher')
CATALOG_URL = 'https://proxy302.com/v1/proxy/ai-isp/catalog'
ORDER_URL = 'https://proxy302.com/product/ai-isp?tier=claude'


def request_json(url, data=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={'User-Agent': 'Proxy302-stock-watcher/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not expose request URLs: Telegram includes the token in its URL.
        raise RuntimeError(f'HTTP error {exc.code}') from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        raise RuntimeError('Network error or invalid JSON response') from None


def request_text(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Proxy302-stock-watcher/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read().decode()
    except urllib.error.HTTPError as exc:
        # Do not expose request URLs: Telegram includes the token in its URL.
        raise RuntimeError(f'HTTP error {exc.code}') from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        raise RuntimeError('Network error or invalid JSON response') from None


# Minified site bundle, names change every deploy; never execute the downloaded code:
#   ident={claude:["DE"]},fn=(a,b)=>b.map( ... ident[ ... .includes( .country ... available:0)
_INDEX_BUNDLE = re.compile(r'/assets/index-[A-Za-z0-9_-]+\.js')
_MAIN_CHUNK = re.compile(r'assets/main-[A-Za-z0-9_-]+\.js')
_BLOCK_LEAD = re.compile(
    r'([A-Za-z_$][\w$]*)\s*=\s*(\{[^{}]*\})\s*,\s*'
    r'[A-Za-z_$][\w$]*\s*=\s*\(\s*[A-Za-z_$][\w$]*\s*,\s*[A-Za-z_$][\w$]*\s*\)\s*=>\s*'
    r'[A-Za-z_$][\w$]*\s*\.\s*map\s*\('
)
# \1 is the map's object ident. The search string is that ident plus the next 200 chars.
_BLOCK_TAIL = re.compile(
    r'\A([A-Za-z_$][\w$]*)[\s\S]{0,200}?\1\s*\['
    r'[\s\S]*?\.includes\s*\(\s*[A-Za-z_$][\w$]*\s*\.\s*country\s*\)'
    r'[\s\S]*?available\s*:\s*0(?!\d)'
)
_BLOCK_ENTRY = re.compile(
    r'\s*(?:([A-Za-z_$][\w$]*)|"((?:\\.|[^"\\])*)"|\'((?:\\.|[^\'\\])*)\')\s*:\s*\[([^\]]*)\]\s*'
)
_COUNTRY_CODE = re.compile(r'\A["\']([A-Za-z]{2})["\']\Z')


def _asset_url(path):
    parts = urllib.parse.urlsplit(ORDER_URL)
    return urllib.parse.urljoin(f'{parts.scheme}://{parts.netloc}/', path)


def _country_codes(body):
    body = body.strip()
    if body == '':
        return set()
    codes = set()
    for part in body.split(','):
        match = _COUNTRY_CODE.fullmatch(part.strip())
        if match is None:
            return None
        codes.add(match.group(1))
    return codes


def parse_block_object(literal):
    """Parse {tier:["CC", ...], ...} without evaluating it. None if it is not that shape."""
    if len(literal) < 2 or literal[0] != '{' or literal[-1] != '}':
        return None
    inner = literal[1:-1]
    if inner.strip() == '':
        return {}
    result = {}
    pos = 0
    while pos < len(inner):
        match = _BLOCK_ENTRY.match(inner, pos)
        if match is None:
            return None
        key = match.group(1)
        if key is None:
            key = match.group(2) if match.group(2) is not None else match.group(3)
        codes = _country_codes(match.group(4))
        if key is None or codes is None:
            return None
        result[key] = codes
        pos = match.end()
        if pos >= len(inner):
            return result
        if inner[pos] != ',':
            return None
        pos += 1
        if pos >= len(inner):
            return None
    return result


def chunk_overrides(js):
    """Return {tier: {country, ...}} from one main chunk, or None if the block is absent."""
    for lead in _BLOCK_LEAD.finditer(js):
        window = lead.group(1) + js[lead.end():lead.end() + 200]
        if _BLOCK_TAIL.match(window) is None:
            continue
        parsed = parse_block_object(lead.group(2))
        if parsed is not None:
            return parsed
    return None


def index_bundle_url(fetch_text):
    html = fetch_text(ORDER_URL)
    match = _INDEX_BUNDLE.search(html)
    if match is None:
        raise RuntimeError('Could not read site restock overrides')
    return _asset_url(match.group(0))


def bundle_overrides(fetch_text, bundle_url):
    index = fetch_text(bundle_url)
    chunks = list(dict.fromkeys(_MAIN_CHUNK.findall(index)))
    if not chunks:
        raise RuntimeError('Could not read site restock overrides')
    for chunk in chunks:
        parsed = chunk_overrides(fetch_text(_asset_url(chunk)))
        if parsed is not None:
            return parsed
    raise RuntimeError('Could not read site restock overrides')


def site_overrides(fetch_text):
    """{tier: {country codes}} the website forces to available 0."""
    return bundle_overrides(fetch_text, index_bundle_url(fetch_text))


def _blocked_sets(blocked):
    return {tier: set(codes) for tier, codes in blocked.items()}


def load_site_overrides(fetch_text, state):
    cached = state.get('site_overrides')
    cached_ok = isinstance(cached, dict) and isinstance(cached.get('blocked'), dict)
    try:
        bundle = index_bundle_url(fetch_text)
        if cached_ok and cached.get('bundle') == bundle:
            return _blocked_sets(cached['blocked'])
        found = bundle_overrides(fetch_text, bundle)
    except RuntimeError:
        if cached_ok:
            print('Warning: site overrides unavailable, using cached', flush=True)
            return _blocked_sets(cached['blocked'])
        raise
    state['site_overrides'] = {
        'bundle': bundle,
        'blocked': {tier: sorted(codes) for tier, codes in found.items()},
    }
    return found


def telegram(method, **data):
    token_path = CONFIG / 'bot-token'
    if token_path.stat().st_mode & 0o077:
        raise RuntimeError('bot-token must have permissions 600')
    token = token_path.read_text().strip()
    if not token or ':' not in token:
        raise RuntimeError('Fill in the protected bot-token file first')
    result = request_json(f'https://api.telegram.org/bot{token}/{method}', data)
    if result.get('ok') is not True:
        raise RuntimeError('Telegram API rejected the request')
    return result['result']


def save_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.write('\n')
    temporary.replace(path)


def claude_cities(payload, blocked=()):
    if payload.get('code') != 0:
        raise RuntimeError('Proxy302 API returned an error')
    data = payload['data']
    if data.get('enabled') is not True:
        raise RuntimeError('Proxy302 AI ISP catalogue is disabled')
    tiers = [tier for tier in data['tiers'] if tier['tier'] == 'claude']
    if len(tiers) != 1:
        raise RuntimeError('Claude tier missing or ambiguous')
    cities = []
    for city in tiers[0]['cities']:
        available = city['available']
        if type(available) is not int or available < 0:
            raise RuntimeError('Invalid stock count')
        if city['country'] in blocked:
            available = 0
        cities.append((city['country'], city['city'], available))
    return cities


def german_stock(payload, blocked=()):
    return [(city, n) for country, city, n in claude_cities(payload, blocked)
            if country == 'DE' and n > 0]


def country_stock(payload, blocked=()):
    """{country: {city: available}}"""
    result = {}
    for country, city, n in claude_cities(payload, blocked):
        cities = result.setdefault(country, {})
        cities[city] = cities.get(city, 0) + n
    return result


COUNTRIES = {'DE': ('🇩🇪', 'Германия'), 'US': ('🇺🇸', 'США'), 'GB': ('🇬🇧', 'Великобритания'),
             'JP': ('🇯🇵', 'Япония'), 'FR': ('🇫🇷', 'Франция'), 'KR': ('🇰🇷', 'Южная Корея'),
             'VN': ('🇻🇳', 'Вьетнам'), 'BR': ('🇧🇷', 'Бразилия')}


def message(rows):
    cities = '\n'.join(f'• {city}: {count} IP' for city, count in rows)
    return ('🚨🚨🚨 ГЕРМАНИЯ ДОСТУПНА! 🚨🚨🚨\n'
            '🇩🇪 Proxy302 AI ISP для Claude: появились IP в Германии!\n\n'
            f'{cities}\n\n👉 {ORDER_URL}\n\n'
            'Это наличие по каталогу; перед оплатой проверь остаток. Ничего не куплено автоматически.')


def digest(stock, restocking=None):
    """restocking maps a site-blocked country to its positive raw API total."""
    restocking = restocking or {}
    blocks = []
    for country, cities in sorted(stock.items(), key=lambda item: -sum(item[1].values())):
        flag, name = COUNTRIES.get(country, ('🏳️', country))
        total = sum(cities.values())
        raw = restocking.get(country, 0)
        if raw:
            lines = [f'{flag} {name}: нет — на сайте «Restocking» (в API: {raw})']
        else:
            lines = [f'{flag} {name}: {total} IP' if total else f'{flag} {name}: нет']
        for city, n in sorted(cities.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f'   • {city}: {n}' if n else f'   • {city}: нет')
        blocks.append('\n'.join(lines))
    return 'Proxy302 AI ISP / Claude: наличие по странам и городам\n\n' + '\n\n'.join(blocks)


DIGEST_INTERVAL = datetime.timedelta(hours=6)


def check(fetch=None, send=None, now=None, fetch_text=None):
    payload = (fetch or (lambda: request_json(CATALOG_URL)))()
    claude_cities(payload)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    state_path = CONFIG / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    overrides = load_site_overrides(fetch_text or request_text, state)
    blocked = overrides.get('claude', set())
    rows = german_stock(payload, blocked)
    totals = country_stock(payload, blocked)
    raw = country_stock(payload)
    restocking = {country: sum(cities.values()) for country, cities in raw.items()
                  if country in blocked and sum(cities.values()) > 0}
    if send is None:
        chat_id = json.loads((CONFIG / 'telegram.json').read_text())['chat_id']
        send = lambda text, silent=False: telegram(
            'sendMessage', chat_id=chat_id, text=text, disable_web_page_preview='true',
            disable_notification='true' if silent else 'false')
    available = bool(rows)
    if available and not state.get('available', False):
        send(message(rows))
    # Persist only after successful delivery, so a failed send is retried.
    state.update(available=available, cities=rows, checked_at=now.isoformat())
    save_json(state_path, state)
    last = state.get('last_digest')
    if last is None or now - datetime.datetime.fromisoformat(last) >= DIGEST_INTERVAL:
        send(digest(totals, restocking), silent=True)
        state['last_digest'] = now.isoformat()
        save_json(state_path, state)
    print(f'Claude/DE: {sum(count for _, count in rows)} available IPs', flush=True)


def connect():
    bot = telegram('getMe')
    updates = telegram('getUpdates', timeout=0)
    chats = {update['message']['chat']['id'] for update in updates
             if update.get('message', {}).get('chat', {}).get('type') == 'private'
             and update['message'].get('text', '').split('@')[0] == '/start'}
    if len(chats) != 1:
        raise RuntimeError('Expected exactly one private /start chat. Open the bot and send /start again.')
    chat_id = chats.pop()
    telegram('sendMessage', chat_id=chat_id,
             text='✅ Telegram подключён к watcher Proxy302. После включения проверок сообщу, '
                  'когда в AI ISP для Claude появится Германия с доступными IP. Покупок не совершаю.')
    save_json(CONFIG / 'telegram.json', {'chat_id': chat_id})
    print(f'Telegram connected: @{bot["username"]}; test notification sent.')


def set_token():
    token = getpass.getpass('Bot token from @BotFather (input hidden): ').strip()
    if ':' not in token:
        raise RuntimeError('That does not look like a bot token')
    path = CONFIG / 'bot-token'
    path.write_text(token + '\n')
    path.chmod(0o600)
    print(f'Token saved to {path}')


def install_cron():
    if not shutil.which('crontab'):
        raise RuntimeError('crontab not found; install cron or schedule watcher.py every 5 minutes yourself')
    line = (f'*/5 * * * * {sys.executable} -B {Path(__file__).resolve()} '
            f'>>{CONFIG / "watcher.log"} 2>&1')
    current = subprocess.run(['crontab', '-l'], capture_output=True, text=True).stdout
    kept = [row for row in current.splitlines() if str(Path(__file__).resolve()) not in row]
    subprocess.run(['crontab', '-'], input='\n'.join(kept + [line]) + '\n', text=True, check=True)
    print('Cron job installed (every 5 minutes):\n' + line)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connect', action='store_true', help='Connect the only private /start chat')
    parser.add_argument('--set-token', action='store_true', help='Save the bot token (hidden prompt)')
    parser.add_argument('--install-cron', action='store_true', help='Run the check every 5 minutes via cron')
    args = parser.parse_args()
    CONFIG.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (CONFIG / 'watcher.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            if args.set_token:
                set_token()
            elif args.connect:
                connect()
            elif args.install_cron:
                install_cron()
            else:
                check()
        except Exception as exc:
            # Deliberately avoid tracebacks and raw network exception strings.
            error = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            print(f'Watcher failed: {error}', flush=True)
            raise SystemExit(1) from None


if __name__ == '__main__':
    main()
