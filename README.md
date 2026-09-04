# Polymarket Weather Bot

An automated trading bot for Polymarket's daily weather markets, with a Telegram
control surface for arming, monitoring, tuning and killing it.

It runs two strategies:

| Strategy | Idea |
|---|---|
| **forecast_edge** | Build our own probability distribution over tomorrow's high/low from an 82-member multi-model weather ensemble (GFS + ECMWF), compare against the order book, and buy buckets the market has underpriced. |
| **copy_trader** | Mirror, at a scaled-down size, the weather-market buys of wallets selected by the research pipeline. |

**It starts in paper mode and refuses to place a live order until you `/arm` it.**

---

## 1. Install

Needs Python 3.10+.

### Ubuntu VPS (recommended)

```bash
git clone <your-repo> polyweather && cd polyweather
sudo bash deploy/install-ubuntu.sh
sudo nano /opt/polyweather/.env          # add your Telegram token + chat id
sudo systemctl start polyweather
sudo journalctl -u polyweather -f        # follow the logs
```

The installer creates a `polyweather` system user, builds the virtualenv under
`/opt/polyweather/.venv`, installs a hardened systemd unit (restart-on-failure,
`ProtectSystem=strict`, `.env` chmod 600 because it holds your private key), and
enables it at boot. Re-run it to upgrade; it never overwrites your `.env` or the
sqlite database.

### Local / development

```bash
python -m venv .venv
source .venv/bin/activate      # Unix;  .venv/Scripts/activate on Windows
pip install -r requirements.txt

cp .env.example .env           # then edit .env
```

## 2. Configure

Minimum to get running in paper mode:

```ini
TRADING_MODE=paper
TELEGRAM_BOT_TOKEN=...         # from @BotFather
TELEGRAM_CHAT_ID=...           # your numeric chat id
```

To find your chat id: message your bot, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

For live trading you additionally need:

```ini
TRADING_MODE=live
PRIVATE_KEY=0x...              # EOA controlling your Polymarket account
FUNDER_ADDRESS=0x...           # your Polymarket deposit/proxy address
SIGNATURE_TYPE=1               # 1 = email/magic wallet, 2 = browser wallet
```

`CLOB_API_KEY` / `CLOB_SECRET` / `CLOB_PASSPHRASE` are derived from the private
key automatically on first run — leave them blank.

## 3. Run

```bash
python main.py             # engine + Telegram
python main.py --scan      # one forecast pass, print the edges, exit
python main.py --no-tg     # engine only, console logging
```

Under systemd, use `systemctl start|stop|restart polyweather` instead; day-to-day
control is via Telegram (`/pause`, `/kill`, `/arm`) without touching the service.

---

## Telegram commands

**Status** — `/status` `/positions` `/trades` `/pnl` `/signals` `/leaders`
**Control** — `/arm` `/disarm` `/pause` `/resume` `/kill` `/revive` `/mode paper|live` `/strategy forecast|copy on|off`
**Tuning** — `/set maxpos 25` `/set daily 200` `/set edge 0.06` `/set copyscale 0.05` `/config` `/reload`

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

## The research pipeline

`research/` finds the wallets that `copy_trader` follows. Run in order:

```bash
python research/01_fetch_events.py      # all 12k weather events -> data/weather_events.json
python research/02_find_candidates.py   # sample markets, collect wallets -> data/candidates.json
python research/03_profile_traders.py   # full history per wallet -> data/top_traders.json
python research/04_classify_traders.py  # strip institution-style wallets
```

`03` computes, per wallet: account age from its first-ever trade, the share of
the last 365 days with at least one trade, and realized PnL **restricted to
weather markets** (from the wallet's own fills plus each market's resolution, so
their sports and crypto books don't contaminate the number).

Selection filters live at the top of `03_profile_traders.py`:

```python
MIN_ACCOUNT_AGE_DAYS = 730      # 2+ years alive
MIN_ACTIVE_DAY_RATIO = 0.80     # trades ~every day
MIN_WEATHER_TRADES   = 200
MIN_WEATHER_PNL      = 0.0
```

### What the research actually found

Run over the full history (Sept 2026), the pipeline covered **12,296 weather
events / 126,691 markets**, sampled 1,967 of them, and saw **120,046 distinct
wallets**. The 150 most weather-active were profiled in full.

**Polymarket's weather category is not two years old.** Event counts by month:

```
2023        4 events   (one-off climate markets)
2024       26 events   (one-off climate markets)
2025-01    39   ...    2025-11    66      (~2 markets/day)
2025-12   278   2026-01  312   2026-02  386
2026-03   864   2026-04 1501   2026-05 1877
2026-06  1697   2026-07 1822   2026-08 2491
```

Daily city-temperature markets only became a real category in **December 2025**.
So no wallet can have a two-year track record *in weather* — the markets did not
exist. The two-year test is therefore applied to **account lifetime** (first
Polymarket activity ≥ 730 days ago), with performance measured over the period
the weather markets have actually existed.

Of the 150 profiled:

| Filter | Passing |
|---|---|
| account age ≥ 730d | 13 |
| trades on ≥80% of observed days | 90 |
| ≥200 weather trades | 140 |
| positive weather PnL | 94 |
| traded within last 10 days | 103 |
| **all of the above** | **4** |
| all except the 2-year age bar | 50 |

**Tier A** — 2+ year old account, trades near-daily, profitable in weather:

| Wallet | Name | Joined | Day% | Wx trades | Wx PnL | ROI | Win |
|---|---|---|---|---|---|---|---|
| `0xf2f6af4f…d5817` | gopfan2 | 2024-08-21 | 81% | 1,386 | **$112,828** | 10.9% | 64% |
| `0x44c1dfe4…3ebc1` | aenews2 | 2024-01-14 | 94% | 715 | $35,860 | 1.1% | 79% |
| `0xaa7a74b8…24d23` | securebet | 2024-07-23 | 80% | 2,828 | $5,664 | 10.0% | 55% |
| `0xa49b6ea0…87054` | 3874110074…| 2024-04-24 | 99% | 1,328 | $3,753 | 7.0% | 51% |

**Tier B** — same bar, younger account (half weight in the leader file).

### Removing institution-style traders

Scale separates cleanly in this data. Average ticket runs $4,445 / $744 / $492
and then falls off a cliff to $77, and those same three wallets are the only ones
above $1M of weather notional. They are desks, not individuals, and
`04_classify_traders.py` removes them outright:

| Removed | Notional | Ticket | ROI | Why |
|---|---|---|---|---|
| aenews2 | $3.2M | $4,445 | 1.1% | desk-scale ticket, thin margin on huge volume |
| meropi | $2.3M | $492 | 0.6% | same shape |
| gopfan2 | $1.0M | $744 | 10.9% | desk-scale ticket and >$1M notional |

Dropping gopfan2 costs the highest raw PnL in the sample ($112,828), which is
the point: that PnL comes from size we cannot mirror, not from an edge we can.

A second, separate test is about *strategy fit* rather than size. `copy_trader`
mirrors BUY legs and holds to resolution, so a wallet that sells out of most of
its positions is not doing what we would be doing. Those wallets are not
discarded — they still carry information — but ride at half weight:

| Halved | Sell share |
|---|---|
| securebet | 53% |
| 387411007… | 42% |

The remaining nine leaders all trade retail-scale tickets ($3–$77) and are
predominantly buy-and-hold.

A note on the activity metric: the trade feed is newest-first and capped, so for
very active wallets it only covers a recent window. Measuring "days active out of
the last 365" against a truncated window scores the *most* active traders lowest,
so the ratio is computed over the window actually observed
(`active_day_ratio_window`), requiring ≥60 days of span.

---

## Layout

```
main.py                     entry point
polyweather/
  config.py                 all settings, from .env
  clients/  gamma.py        market discovery
            dataapi.py      public trade/position feeds
            clob.py         order books + authenticated order placement
  weather/  cities.py       57 cities -> station coords, tz, quoted unit
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
research/                   trader discovery + style classification
deploy/   install-ubuntu.sh, polyweather.service
tests/                      bucket maths, sizing, risk limits
```

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
fake edges on tail buckets. Before trading live, run in paper mode long enough
to compare `model_prob` against realised outcomes and tune it.

---

## Caveats

- **The edges are unvalidated.** A first live scan produced signals as large as
  +33%. Edges that big usually mean a thin resting order or a miscalibrated
  bandwidth, not free money. Paper-trade first.
- **Copy trading is inherently lagged.** You see a leader's fill after it
  happened. `COPY_MAX_AGE_SEC` and the 4-cent chase guard limit the damage, but
  you will systematically get worse prices than the leader.
- **Resolution source.** Polymarket resolves against a specific station; the
  coordinates in `cities.py` target the primary reporting station, but verify
  against the market's own resolution text for any city you trade heavily.
- Past performance of the copied wallets does not predict their future results.
