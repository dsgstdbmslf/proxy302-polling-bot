# proxy302-polling-bot

Telegram notifications about Claude AI ISP proxy stock on [Proxy302](https://proxy302.com).

- A loud 🚨 alert as soon as **Germany** becomes available.
- A silent digest every 6 hours: stock for every country, split by city.

It reads the public catalogue only. No Proxy302 account, no purchases. Python 3 standard
library only, nothing to `pip install`.

## Setup

Requires Linux/macOS with `python3` and `cron`.

```sh
git clone https://github.com/dsgstdbmslf/proxy302-polling-bot.git
cd proxy302-polling-bot

# 1. Create a bot with @BotFather (/newbot) and paste its token (input is hidden)
python3 watcher.py --set-token

# 2. Open your new bot in Telegram and send /start, then:
python3 watcher.py --connect

# 3. Check every 5 minutes via cron
python3 watcher.py --install-cron
```

Settings and state are stored outside the repo in `~/.config/proxy302-watcher/`
(or `$XDG_CONFIG_HOME/proxy302-watcher/`, or `$PROXY302_WATCHER_DIR`), mode 700,
token mode 600. Nothing secret is ever written to the repository.

The bot only **sends** messages to the one chat that sent `/start`; it does not read
or answer anyone else. `--connect` refuses to continue unless exactly one private
`/start` chat exists. If a token leaks, revoke it with `/revoke` in @BotFather.

Run as the same user for all steps (cron jobs run as the user who installed them).

## Behaviour

- Germany alert on first detection and on each transition from unavailable to available;
  no repeats while stock stays positive.
- Also reads the website's per-tier country block (Germany for Claude right now) and treats
  those countries as out of stock, with a cached fallback if the site cannot be read.
- Digest on the first run, then every 6 hours, sent silently.
- Failed source requests keep the previous state; failed sends are retried next run.
- A file lock prevents concurrent runs; state is replaced atomically.
- Delivery is at-least-once: a crash between sending and saving state can duplicate a message.
- Polling is not an availability guarantee; the provider's catalogue may refresh slower.

## Test

```sh
python3 -B -m unittest -v
```
