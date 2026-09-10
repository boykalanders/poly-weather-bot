# Polymarket Weather Bot

An automated trading bot for Polymarket's daily weather markets, with a Telegram
control surface for arming, monitoring, tuning and killing it.

One strategy: **copy_trader** mirrors, at scaled-down size, the weather-market
buys of leader wallets you list in `.env`.

**It starts in paper mode and refuses to place a live order until you `/arm` it.**

---

## Deploy

### Ubuntu VPS

Needs Python 3.10+ (Ubuntu 22.04 / 24.04 are fine as shipped).

```bash
git clone <your-repo> polyweather && cd polyweather
sudo bash deploy/install-ubuntu.sh
sudo nano /opt/polyweather/.env          # Telegram token + chat id
sudo systemctl start polyweather
sudo journalctl -u polyweather -f        # follow the logs
```

The installer creates a `polyweather` system user, builds a virtualenv at
`/opt/polyweather/.venv`, installs `data/top_traders.json`, and enables a
hardened systemd unit (restart-on-failure, `ProtectSystem=strict`, `.env`
chmod 600 because it holds your private key).

Re-run it to upgrade. It never overwrites your `.env` or the sqlite database,
but it does refresh `data/top_traders.json`.

Service control:

```bash
sudo systemctl restart polyweather
sudo systemctl stop polyweather
sudo systemctl status polyweather
```

Day-to-day control is via Telegram (`/pause`, `/kill`, `/arm`) — no need to
touch the service.

### macOS

Needs Python 3.10+. The `python3` Apple ships is 3.9, so install a newer one
first: `brew install python@3.12`.

```bash
git clone <your-repo> poly-weather && cd poly-weather
bash deploy/install-macos.sh       # no sudo
nano .env                          # Telegram token + chat id
launchctl kickstart -k gui/$UID/com.polyweather.bot
tail -f logs/bot.log
```

The installer builds `.venv`, chmods `.env` to 600, and writes a launchd agent
at `~/Library/LaunchAgents/com.polyweather.bot.plist` that runs at login and
restarts on crash. It runs as you, from the checkout — there is no `/opt` copy
and no service account, because a LaunchAgent runs as the logged-in user
anyway. Re-run it to upgrade.

```bash
launchctl kickstart -k gui/$UID/com.polyweather.bot   # restart, e.g. after .env edits
launchctl print gui/$UID/com.polyweather.bot          # status
launchctl bootout gui/$UID/com.polyweather.bot        # stop and unload
```

**A Mac is not a VPS.** A LaunchAgent runs only while you are logged in, and
the bot stops when the machine sleeps — a sleeping laptop misses the leader
fills it exists to mirror, and `COPY_MAX_AGE_SEC` means a trade found late is
skipped rather than chased. For unattended running, keep it awake:

```bash
caffeinate -dimsu -w $(pgrep -f 'polyweather|main.py' | head -1)
```

or turn off sleep in System Settings → Battery / Energy Saver. On a laptop that
closes, treat this as a foreground tool you start when you want it, not a
service.

### Fly.io

Runs the bot as a persistent worker with the sqlite database on a volume.

```bash
fly launch --no-deploy --copy-config      # edit `app` in fly.toml first
fly volumes create polyweather_data -s 1 -r lhr
fly secrets set TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...                 COPY_WALLETS=0xabc...:KickstandBot:1.0
fly deploy
fly logs
```

**Run exactly one machine.** Fly often provisions two by default, and two
copies of this bot means every leader fill is mirrored twice, both instances
race on the same daily counter, and Telegram rejects the second long-poll with
a 409 conflict. The volume can only attach to one machine anyway:

```bash
fly scale count 1
fly status                                # confirm: 1 machine
```

Config comes from `fly secrets`, not a `.env` — no `.env` ships in the image
(see `.dockerignore`), and pydantic-settings ranks environment variables above
the file. `/reload` still works: `copy_trader` falls back to the process
environment when there is no file on disk.

Two things the Dockerfile handles that are easy to get wrong:

- The volume mounts at `/app/data` and **hides whatever the image had there**,
  so `data/top_traders.json` is copied to `/app/seed/` and `COPY_WALLETS_FILE`
  points at that. Without it the leader fallback vanishes the moment a volume
  is attached, and copy trading follows nobody.
- The database must be on the volume. It holds the daily notional counter, the
  open positions, the leader-trade dedup keys and the kill-switch flag — losing
  it on redeploy silently resets every risk limit at once.

There is no `[http_service]`: the bot never listens on a port, so there is
nothing to route to and no health check to pass.

### Render.com

Same shape as Fly, using the committed `render.yaml` and the same Dockerfile.

1. Push the repo, then in the Render dashboard: **New → Blueprint** and pick it.
2. It prompts for `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and `COPY_WALLETS`
   (they are marked `sync: false`, so they are never stored in the repo).
3. Deploy, and watch the service logs.

The blueprint declares a **Background Worker**, not a Web Service. That is the
one choice worth understanding: Render health-checks a Web Service on an HTTP
port, and this bot never opens one — it long-polls Telegram outbound. Deployed
as a Web Service it would be killed for failing a health check it can never
pass.

**Both the worker and the disk are paid-plan features.** Render has no free
tier that keeps a process alive with persistent storage, so this is not a way
to run the bot for nothing.

The disk mounts at `/app/data` for the sqlite database, for exactly the reasons
in the Fly section — and it hides the image's copy of the leader list the same
way, which is why `COPY_WALLETS_FILE` points at `/app/seed/`. Attaching a disk
also pins the service to a single instance, so unlike Fly there is no scale
count to remember.

### Windows

Registers a Scheduled Task that starts at logon and runs windowless.

```powershell
cd C:\path\to\poly-weather                      # every path below is relative
powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1
notepad .env                                    # Telegram token + chat id
Start-ScheduledTask -TaskName PolyWeatherBot    # only after the installer ran
Get-Content logs\bot.log -Wait -Tail 20
```

Control:

```powershell
Get-ScheduledTask   -TaskName PolyWeatherBot    # status
Stop-ScheduledTask  -TaskName PolyWeatherBot
Start-ScheduledTask -TaskName PolyWeatherBot    # restart, e.g. after .env edits
Unregister-ScheduledTask -TaskName PolyWeatherBot
```

It runs under `pythonw.exe`, so there is no console window and **`logs\bot.log`
is the only place output appears** — the file handler works with no console
streams at all, so nothing is lost.

Four task settings are doing real work, and each fixes a default that would
otherwise bite:

| Setting | Why |
|---|---|
| `ExecutionTimeLimit 0` | the default kills a task after three days |
| `MultipleInstances IgnoreNew` | two copies would mirror every fill twice and collide on Telegram's long poll |
| `AllowStartIfOnBatteries` | Windows refuses to start a task on battery by default |
| `DontStopIfGoingOnBatteries` | and stops a running one when you unplug |

This is a Scheduled Task, not a true Windows service. A real service needs a
wrapper such as NSSM or WinSW to supervise a non-service binary, and buys only
the ability to run with nobody logged in — which a desktop that sleeps will not
deliver anyway.

**A sleeping PC stops the bot**, exactly as on the Mac: at roughly two copy
signals a day, and with `COPY_MAX_AGE_SEC` discarding anything found late, an
overnight sleep misses most of them. Set the machine to never sleep, or treat
this as something you start deliberately rather than a service.

### Running it by hand

```bash
python main.py             # engine + Telegram
python main.py --scan      # one leader poll, print what would be copied, exit
python main.py --no-tg     # engine only, console logging
```

---

## Configure

Copy `.env.example` to `.env`. Minimum for paper mode:

```ini
TRADING_MODE=paper
TELEGRAM_BOT_TOKEN=...         # from @BotFather
TELEGRAM_CHAT_ID=...           # your numeric chat id
```

To find your chat id: message your bot, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

For live trading:

```ini
TRADING_MODE=live
PRIVATE_KEY=0x...              # EOA controlling your Polymarket account
FUNDER_ADDRESS=0x...           # your Polymarket deposit/proxy address
SIGNATURE_TYPE=1               # 1 = legacy proxy wallet, 2 = legacy safe wallet
```

**Check your wallet type first.** It depends on when the account was created,
not how you log in: every account created since 2026-05-04 is a *Deposit
Wallet* (`SIGNATURE_TYPE=3`); older email/Google accounts are Proxy Wallets
(`1`); older MetaMask/Rabby accounts are Safe Wallets (`2`). All four types
sign through `py-clob-client-v2`.

**The bot needs the V2 CLOB client.** Polymarket moved to CTF Exchange V2 and
dropped V1-signed orders on 2026-04-28 — a new order struct, new exchange
contracts, and pUSD instead of USDC.e as collateral. The legacy
`py-clob-client` still signs the old struct, and the venue rejects it with
`invalid order version, please use the latest clob-client`. If you see that,
your install predates this change:

```bash
pip uninstall -y py-clob-client
pip install -r requirements.txt
```

The V2 client asks the server for the current order version and re-signs on a
mismatch, so a future version bump should not strand the bot the same way.

**Collateral is pUSD.** Funds still held as USDC.e must be wrapped before they
can back an order. Depositing through polymarket.com's Bridge wraps
automatically; otherwise an order fails with `not enough balance / allowance`.

`CLOB_API_KEY` / `CLOB_SECRET` / `CLOB_PASSPHRASE` are derived from the private
key on first run — leave them blank.

---

## Telegram commands

**Status** — `/status` `/positions` `/trades` `/pnl` `/signals` `/leaders`

**Control** — `/arm` `/disarm` `/pause` `/resume` `/kill` `/revive`
`/mode paper|live` `/strategy copy on|off`

**Tuning** — `/set maxpos 25` `/set daily 200` `/set copyscale 0.05`
`/config` `/reload`

You get a push message on every fill, every settlement (win/loss + PnL), every
loop error, and a daily summary at `HEARTBEAT_HOUR_UTC`.

---

## Risk controls

Every order proposal goes through `RiskManager.check()` — nothing else in the
bot is allowed to size a trade.

- `MAX_POSITION_USDC` — per market
- `MAX_DAILY_NOTIONAL_USDC` — per UTC day, all markets
- `MAX_OPEN_POSITIONS`
- `MAX_DAILY_LOSS_USDC` — **trips the kill switch** and stops the day
- `KELLY_FRACTION` — fractional Kelly sizing (0.25 default; 1.0 would be reckless)
- `/kill` — engages the kill switch and cancels all resting orders

**Minimum order size.** `MIN_ORDER_USDC` (default $1) and `MIN_ORDER_SHARES`
(default 5) are the venue's floors. An order sized below either is **rounded up
to the minimum, not skipped** — with `COPY_SCALE=0.05`, a $200 leader trade
mirrors to $10, but a $20 one mirrors to $1.00 of dust that would otherwise be
dropped silently, leaving copy trading looking alive while placing nothing.

Which floor binds depends on price: at $0.02 a share the $1 notional needs 50
shares; at $0.90 the 5-share floor already implies $4.50. Rounding up never
overrides a risk limit — if the minimum order would breach the per-market or
daily cap, it is refused with a logged reason instead.

Live mode has two independent gates: `TRADING_MODE=live` **and** `/arm`.
Switching mode auto-disarms.

---

## How a copy signal arrives

Two paths, both ending in the same filter chain:

1. **The activity stream** — `wss://ws-live-data.polymarket.com`, topic
   `activity` / type `trades`. Every fill on Polymarket, each carrying the
   trader's `proxyWallet`, roughly 60 events a second. We take the firehose and
   match wallets locally, because the server-side filters accept only
   `event_slug` / `market_slug`, never an address. A leader's trade reaches us
   about a second after it happens.
2. **The backstop poll** — the data-api every `COPY_POLL_INTERVAL_SEC`, for
   whatever a disconnect dropped. `seen_leader_trade` dedups the overlap, so a
   trade delivered twice is mirrored once.

Latency is not cosmetic here. The 4-cent chase guard refuses to follow a leader
once the market has moved past their fill, so an extra 45 seconds is the
difference between a mirror and a skip.

Neither the CLOB `market` socket nor the CLOB `user` socket can do this job:
the first streams books for token ids you name and never says who traded, and
the second is authenticated and scoped to your own account.

## Copy-trade leaders

Managed entirely in `.env`. Add, remove or re-weight a wallet, then send
`/reload` in Telegram — no restart, no redeploy.

```ini
COPY_WALLETS=0xca1f9b9d...cd282:KickstandBot:1.0,0xaa7a74b8...24d23:securebet:1.0
```

Format is `wallet[:name][:weight]`, comma separated:

| Entry | Meaning |
|---|---|
| `0xabc...123` | follow at full weight, labelled by address |
| `0xabc...123:whale` | named, full weight |
| `0xabc...123:whale:0.5` | named, half size |
| `0xabc...123:0.5` | weight only, no name |

`weight` multiplies `COPY_SCALE` for that wallet, so `0.5` mirrors half as much.
Addresses are case-insensitive, whitespace is trimmed, duplicates are dropped,
and entries starting with `#` are ignored.

An invalid entry is logged and skipped. If *every* entry is invalid the bot
follows **no one** rather than falling back to the JSON file — a typo must never
resurrect wallets you thought you had removed. `/reload` tells you what it
loaded, and `/leaders` shows the active list and where it came from.

Leave `COPY_WALLETS` empty to fall back to `data/top_traders.json`, which uses
`{"traders": [{"wallet": "0x…", "name": "…", "weight": 1.0}]}`.

### Currently configured

Selected from 120,046 wallets seen across Polymarket's full weather history
(12,296 events / 126,691 markets):

| Leader | Copied buys | Hold-to-resolution ROI | Win | Sells | Daily-weather |
|---|---|---|---|---|---|
| **KickstandBot** | 125 | 10.0% | 76% | 0% | 100% |
| **securebet** | 98 | 17.3% | 91% | 53% | 100% |

Both trade only daily city-temperature markets. The metrics come from simulating
what the bot actually does — mirror BUYs over `COPY_MIN_LEADER_NOTIONAL` and hold
to resolution — not from the wallets' headline stats, which rank very differently.

Two caveats:

- **KickstandBot** is the active side (~2 copyable buys/day, and a 0% sell share
  so it matches the bot's hold-to-resolution behaviour exactly), but its copyable
  flow only began 11 days before selection.
- **securebet** has the better and far longer record (17.3% across 18 months),
  but had made no copyable buy since 2026-08-07.

Expect roughly 2 copy signals a day. That is the whole of the bot's activity
now that forecast_edge is gone — long quiet stretches are normal, not a fault.

---

## Layout

```
main.py                     entry point
polyweather/
  config.py                 all settings, from .env
  clients/  gamma.py        market discovery
            rtds.py         realtime activity stream (leader fills)
            dataapi.py      public trade/position feeds
            clob.py         order books + authenticated order placement
            http.py         retrying HTTP wrapper
  strategy/ copy_trader.py  leader polling, weather-slug filter, mirroring
  engine/   risk.py         limits, sizing, kill switch
            executor.py     paper + live execution
            bot.py          loops, settlement, status
  store/db.py               sqlite: trades, positions, daily counters
  tg/       app.py          command handlers
            notifier.py     thread -> asyncio bridge for alerts
deploy/   install-ubuntu.sh + polyweather.service (systemd)
          install-macos.sh (launchd agent)
          install-windows.ps1 (scheduled task)
Dockerfile                  container image (Fly.io, Render)
fly.toml, render.yaml       host blueprints
tests/                      sizing, risk limits, leader parsing
data/top_traders.json       copy-trade leaders
```

Run the tests with `python -m pytest tests/ -q`.

---

## Caveats

- **Copy trading is inherently lagged.** You see a leader's fill after it
  happened. `COPY_MAX_AGE_SEC` and the 4-cent chase guard limit the damage, but
  you will systematically get worse prices than the leader.
- **Resolution source.** Polymarket resolves against a specific station; the
  coordinates in `cities.py` target the primary reporting station, but verify
  against the market's own resolution text for any city you trade heavily.
- Past performance of the copied wallets does not predict their future results.
