# Polymarket Weather Bot

An automated trading bot for Polymarket's daily weather markets, with a Telegram
control surface for arming, monitoring, tuning and killing it.

Two strategies:

| Strategy | Idea |
|---|---|
| **forecast_edge** | Build a probability distribution over the day's high/low from an 82-member multi-model weather ensemble (GFS + ECMWF), compare against the order book, and buy buckets the market has underpriced. |
| **copy_trader** | Mirror, at scaled-down size, the weather-market buys of the wallets in `data/top_traders.json`. |

**It starts in paper mode and refuses to place a live order until you `/arm` it.**

---

## Deploy on an Ubuntu VPS

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

### Running it by hand

```bash
python main.py             # engine + Telegram
python main.py --scan      # one forecast pass, print the edges, exit
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
SIGNATURE_TYPE=1               # 1 = email/magic wallet, 2 = browser wallet
```

`CLOB_API_KEY` / `CLOB_SECRET` / `CLOB_PASSPHRASE` are derived from the private
key on first run — leave them blank.

---

## Telegram commands

**Status** — `/status` `/positions` `/trades` `/pnl` `/signals` `/leaders`

**Control** — `/arm` `/disarm` `/pause` `/resume` `/kill` `/revive`
`/mode paper|live` `/strategy forecast|copy on|off`

**Tuning** — `/set maxpos 25` `/set daily 200` `/set edge 0.06`
`/set copyscale 0.05` `/config` `/reload`

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

Live mode has two independent gates: `TRADING_MODE=live` **and** `/arm`.
Switching mode auto-disarms.

---

## Copy-trade leaders

`data/top_traders.json` holds the wallets `copy_trader` follows. Edit it and send
`/reload` in Telegram — no restart needed. Format:

```json
{"traders": [{"wallet": "0x…", "name": "label", "weight": 1.0}]}
```

`weight` scales `COPY_SCALE` for that wallet. Fields prefixed `_` are notes and
are ignored by the bot.

Currently configured, selected from 120,046 wallets seen across Polymarket's
full weather history (12,296 events / 126,691 markets):

| Leader | Copied buys | Hold-to-resolution ROI | Win | Sells | Daily-weather |
|---|---|---|---|---|---|
| **KickstandBot** | 125 | 10.0% | 76% | 0% | 100% |
| **securebet** | 98 | 17.3% | 91% | 53% | 100% |

Both trade only daily city-temperature markets. The metrics come from simulating
what the bot actually does — mirror BUYs over `COPY_MIN_LEADER_NOTIONAL` and hold
to resolution — not from the wallets' headline stats, which rank very differently.

Two caveats, also recorded in the file:

- **KickstandBot** is the active side (~2 copyable buys/day, and a 0% sell share
  so it matches the bot's hold-to-resolution behaviour exactly), but its copyable
  flow only began 11 days before selection.
- **securebet** has the better and far longer record (17.3% across 18 months),
  but had made no copyable buy since 2026-08-07.

Expect roughly 2 copy signals a day. **Keep `ENABLE_FORECAST_EDGE=true`** —
copy trading alone will trade rarely.

---

## How the probability model works

Raw ensembles are **under-dispersed** — members cluster tighter than reality, so
counting members per bucket gives over-confident probabilities. Instead each
member gets a Gaussian kernel and the CDFs are averaged
(`buckets.bucket_probability`). Bandwidth is
`max(floor, 0.35 × ensemble_spread)`, with the floor (0.8°F / 0.45°C)
representing irreducible station and rounding noise.

Buckets are half-open intervals on the *rounded* reported temperature: `"24°C"`
means the reported high rounds to 24, i.e. `[23.5, 24.5)`. The ladder is
exhaustive and mutually exclusive, so probabilities are renormalised to sum to 1.

**The bandwidth is the single most important number in this bot and it is not
yet calibrated against outcomes.** Too narrow and the model manufactures huge
fake edges on tail buckets. Run in paper mode long enough to compare
`model_prob` against realised outcomes before going live.

---

## Layout

```
main.py                     entry point
polyweather/
  config.py                 all settings, from .env
  clients/  gamma.py        market discovery
            dataapi.py      public trade/position feeds
            clob.py         order books + authenticated order placement
            http.py         retrying HTTP wrapper
  weather/  cities.py       58 cities -> station coords, tz, quoted unit
            providers.py    Open-Meteo ensemble, cached + rate-paced
            buckets.py      bucket parsing + kernel-smoothed probabilities
  strategy/ forecast_edge.py
            copy_trader.py
  engine/   risk.py         limits, sizing, kill switch
            executor.py     paper + live execution
            bot.py          loops, settlement, status
  store/db.py               sqlite: trades, positions, daily counters
  tg/       app.py          command handlers
            notifier.py     thread -> asyncio bridge for alerts
deploy/   install-ubuntu.sh, polyweather.service
tests/                      bucket maths, sizing, risk limits
data/top_traders.json       copy-trade leaders
```

Run the tests with `python -m pytest tests/ -q`.

---

## Caveats

- **The forecast edges are unvalidated.** Live scans have produced signals as
  large as +30%. Edges that big usually mean a thin resting order or a
  miscalibrated bandwidth, not free money. Paper-trade first.
- **Copy trading is inherently lagged.** You see a leader's fill after it
  happened. `COPY_MAX_AGE_SEC` and the 4-cent chase guard limit the damage, but
  you will systematically get worse prices than the leader.
- **Resolution source.** Polymarket resolves against a specific station; the
  coordinates in `cities.py` target the primary reporting station, but verify
  against the market's own resolution text for any city you trade heavily.
- Past performance of the copied wallets does not predict their future results.
