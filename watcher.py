#!/usr/bin/env python3
"""Watch public Proxy302 AI ISP (Claude) inventory and notify via Telegram; never purchases anything."""
import argparse
import datetime
import fcntl
import getpass
import json
import os
from pathlib import Path
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


def claude_cities(payload):
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
        cities.append((city['country'], city['city'], available))
    return cities


def german_stock(payload):
    return [(city, n) for country, city, n in claude_cities(payload) if country == 'DE' and n > 0]


def country_stock(payload):
    """{country: {city: available}}"""
    result = {}
    for country, city, n in claude_cities(payload):
        cities = result.setdefault(country, {})
        cities[city] = cities.get(city, 0) + n
    return result


COUNTRIES = {'DE': ('🇩🇪', 'Германия'), 'US': ('🇺🇸', 'США'), 'GB': ('🇬🇧', 'Великобритания'),
             'JP': ('🇯🇵', 'Япония'), 'FR': ('🇫🇷', 'Франция'), 'KR': ('🇰🇷', 'Южная Корея')}


def message(rows):
    cities = '\n'.join(f'• {city}: {count} IP' for city, count in rows)
    return ('🚨🚨🚨 ГЕРМАНИЯ ДОСТУПНА! 🚨🚨🚨\n'
            '🇩🇪 Proxy302 AI ISP для Claude: появились IP в Германии!\n\n'
            f'{cities}\n\n👉 {ORDER_URL}\n\n'
            'Это наличие по каталогу; перед оплатой проверь остаток. Ничего не куплено автоматически.')


def digest(stock):
    blocks = []
    for country, cities in sorted(stock.items(), key=lambda item: -sum(item[1].values())):
        flag, name = COUNTRIES.get(country, ('🏳️', country))
        total = sum(cities.values())
        lines = [f'{flag} {name}: {total} IP' if total else f'{flag} {name}: нет']
        for city, n in sorted(cities.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f'   • {city}: {n}' if n else f'   • {city}: нет')
        blocks.append('\n'.join(lines))
    return 'Proxy302 AI ISP / Claude: наличие по странам и городам\n\n' + '\n\n'.join(blocks)


DIGEST_INTERVAL = datetime.timedelta(hours=6)


def check(fetch=None, send=None, now=None):
    payload = (fetch or (lambda: request_json(CATALOG_URL)))()
    rows = german_stock(payload)
    totals = country_stock(payload)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    state_path = CONFIG / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
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
        send(digest(totals), silent=True)
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
