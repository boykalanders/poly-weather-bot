"""Profile candidate wallets and pick the ones worth copying.

For each candidate we pull the wallet's entire trade history from data-api and
compute:

  * account age          -- first trade ever (the "2+ years alive" filter)
  * daily activity       -- share of the last 365 days with >=1 trade
  * weather PnL          -- realized P&L restricted to weather markets, computed
                            from the wallet's own fills plus each market's
                            resolution, so it is not distorted by their sports
                            or crypto books
  * weather consistency  -- win rate across resolved weather markets

Output: data/trader_profiles.json and data/top_traders.json (the bot's leaders).
"""
import collections
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "research")
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402

from common import DATA, get  # noqa: E402

NOW = int(time.time())
YEAR = 365 * 86400

# --- selection thresholds -------------------------------------------------
MIN_ACCOUNT_AGE_DAYS = 730      # "more than 2 years lifelong"
MIN_ACTIVE_DAY_RATIO = 0.80     # "trades every day", over the observed window
MIN_WEATHER_TRADES = 200
MIN_WEATHER_RESOLVED = 50
MIN_WEATHER_PNL = 0.0

MAX_CANDIDATES = 150            # profile only the most weather-active candidates
MAX_TRADE_PAGES = 20            # 500 trades/page -> 10k most recent trades
CHECKPOINT = "data/profiles.jsonl"   # streamed so a crash never loses work


def load_weather_universe():
    """condition_id -> (event_slug, winning_outcome_index or None)."""
    events = json.load(open("data/weather_events.json"))
    universe = {}
    slugs = set()
    for e in events:
        slugs.add(e["slug"])
        for m in e.get("markets", []):
            cid = m.get("c")
            if not cid:
                continue
            winner = None
            op = m.get("op")
            if isinstance(op, str):
                try:
                    op = json.loads(op)
                except ValueError:
                    op = None
            if op and m.get("closed"):
                prices = [float(x) for x in op]
                # A resolved market settles to exactly 1/0; anything else is
                # still live or was voided, so we refuse to score it.
                if max(prices) > 0.99 and min(prices) < 0.01:
                    winner = prices.index(max(prices))
            universe[cid] = (e["slug"], winner)
    return universe, slugs


def fetch_all_trades(wallet):
    out = []
    for page in range(MAX_TRADE_PAGES):
        d = get(f"{DATA}/trades?user={wallet}&limit=500&offset={page * 500}")
        if not d:
            break
        out += d
        if len(d) < 500:
            break
    return out


def first_activity_ts(wallet):
    """Timestamp of the wallet's earliest-ever activity.

    The trade feed is newest-first, so a wallet with more than MAX_TRADE_PAGES
    of history would have its birth date truncated away -- exactly the wallets
    the 2-year filter cares about.  Ask the activity feed directly instead.
    """
    try:
        d = get(f"{DATA}/activity?user={wallet}&limit=1&sortBy=TIMESTAMP&sortDirection=ASC")
    except Exception:
        return 0
    if d and isinstance(d, list):
        return int(d[0].get("timestamp") or 0)
    return 0


def profile(args):
    wallet, name, universe = args
    trades = fetch_all_trades(wallet)
    if not trades:
        return None

    last_ts = max(int(t["timestamp"]) for t in trades)
    truncated = len(trades) >= MAX_TRADE_PAGES * 500
    # Authoritative account birth date, independent of trade-feed truncation.
    first_ts = first_activity_ts(wallet) or min(int(t["timestamp"]) for t in trades)

    # The trade feed is newest-first and capped at MAX_TRADE_PAGES, so for very
    # active wallets it covers only a recent window.  Measuring "days active out
    # of the last 365" against a truncated window would score the *most* active
    # traders lowest, so the daily-activity ratio is measured over the window we
    # actually observed.
    window_start = min(int(t["timestamp"]) for t in trades)

    all_days = set()
    recent_days = set()
    wx_days = set()
    wx_trades = 0
    wx_notional = 0.0
    # (condition_id, outcome_index) -> [cash_flow, net_shares]
    legs = collections.defaultdict(lambda: [0.0, 0.0])

    for t in trades:
        ts = int(t["timestamp"])
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        all_days.add(day)
        if ts > NOW - YEAR:
            recent_days.add(day)

        cid = t.get("conditionId")
        if cid not in universe:
            continue

        size = float(t.get("size") or 0)
        price = float(t.get("price") or 0)
        idx = int(t.get("outcomeIndex") or 0)
        wx_trades += 1
        wx_notional += size * price
        wx_days.add(day)

        leg = legs[(cid, idx)]
        if (t.get("side") or "").upper() == "BUY":
            leg[0] -= size * price
            leg[1] += size
        else:
            leg[0] += size * price
            leg[1] -= size

    # ---- realized weather PnL over resolved markets only ----
    pnl = 0.0
    resolved_legs = 0
    wins = 0
    for (cid, idx), (cash, shares) in legs.items():
        winner = universe[cid][1]
        if winner is None:
            continue
        resolved_legs += 1
        leg_pnl = cash + shares * (1.0 if idx == winner else 0.0)
        pnl += leg_pnl
        if leg_pnl > 0:
            wins += 1

    age_days = (NOW - first_ts) / 86400
    span_days = max(1.0, min(age_days, 365.0))
    # Days spanned by the fetched trades, and the share of them with a trade.
    window_days = max(1.0, (last_ts - window_start) / 86400)
    window_ratio = min(1.0, len(all_days) / window_days)

    return {
        "wallet": wallet,
        "name": name,
        "first_trade": datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        "last_trade": datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        "account_age_days": round(age_days, 1),
        "n_trades_total": len(trades),
        "history_truncated": truncated,
        "active_days_total": len(all_days),
        "active_days_365": len(recent_days),
        "active_day_ratio_365": round(len(recent_days) / span_days, 3),
        "window_start": datetime.fromtimestamp(window_start, tz=timezone.utc).strftime("%Y-%m-%d"),
        "window_days": round(window_days, 1),
        "active_day_ratio_window": round(window_ratio, 3),
        "days_since_last_trade": round((NOW - last_ts) / 86400, 1),
        "weather_trades": wx_trades,
        "weather_notional": round(wx_notional, 2),
        "weather_active_days": len(wx_days),
        "weather_resolved_legs": resolved_legs,
        "weather_pnl": round(pnl, 2),
        "weather_leg_win_rate": round(wins / resolved_legs, 3) if resolved_legs else None,
        "weather_roi": round(pnl / wx_notional, 4) if wx_notional > 0 else None,
    }


def main():
    universe, _ = load_weather_universe()
    print(f"weather universe: {len(universe)} markets", flush=True)

    cands = json.load(open("data/candidates.json"))
    # Most weather-active first: we want people who live in these markets.
    cands.sort(key=lambda r: (-r["n_markets"], -r["notional_sampled"]))
    picked = cands[:MAX_CANDIDATES]
    print(f"profiling {len(picked)} candidates", flush=True)

    # Resume: anything already checkpointed is not re-fetched.
    profiles = []
    done = set()
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                profiles.append(rec)
                done.add(rec["wallet"])
        print(f"resuming: {len(done)} already profiled", flush=True)

    jobs = [(c["wallet"], c.get("name", ""), universe)
            for c in picked if c["wallet"] not in done]
    print(f"{len(jobs)} left to fetch", flush=True)

    def safe(job):
        try:
            return profile(job)
        except Exception as e:  # noqa: BLE001
            print(f"  !! {job[0][:12]} {type(e).__name__}: {e}", flush=True)
            return None

    with open(CHECKPOINT, "a", encoding="utf-8") as ckpt:
        with ThreadPoolExecutor(max_workers=12) as ex:
            futures = {ex.submit(safe, j): j for j in jobs}
            for i, fut in enumerate(as_completed(futures), 1):
                p = fut.result()
                if p:
                    profiles.append(p)
                    ckpt.write(json.dumps(p) + chr(10))
                    ckpt.flush()
                if i % 25 == 0:
                    print(f"  {i}/{len(jobs)}", flush=True)

    profiles.sort(key=lambda r: -r["weather_pnl"])
    json.dump(profiles, open("data/trader_profiles.json", "w"), indent=1)
    print(f"\nprofiled {len(profiles)} wallets -> data/trader_profiles.json")

    # ---------------- apply the selection filters ----------------
    def passes(p):
        return (
            p["account_age_days"] >= MIN_ACCOUNT_AGE_DAYS
            and p["active_day_ratio_window"] >= MIN_ACTIVE_DAY_RATIO
            and p["window_days"] >= 60          # judged over a meaningful span
            and p["weather_trades"] >= MIN_WEATHER_TRADES
            and p["weather_resolved_legs"] >= MIN_WEATHER_RESOLVED
            and p["weather_pnl"] > MIN_WEATHER_PNL
            and p["days_since_last_trade"] <= 3
        )

    selected = [p for p in profiles if passes(p)]
    print(f"\n{len(selected)} wallet(s) pass every filter:")
    print(f"  age>={MIN_ACCOUNT_AGE_DAYS}d, active>={MIN_ACTIVE_DAY_RATIO:.0%} of days, "
          f"weather trades>={MIN_WEATHER_TRADES}, PnL>0")

    hdr = (f"{'wallet':44s} {'name':16s} {'first':11s} {'age_d':>6s} "
           f"{'act%':>5s} {'wxTr':>6s} {'wxPnL':>12s} {'ROI':>7s} {'win':>5s}")
    print("\n" + hdr)
    print("-" * len(hdr))
    for p in selected[:40]:
        print(f"{p['wallet']:44s} {(p['name'] or '')[:16]:16s} {p['first_trade']:11s} "
              f"{p['account_age_days']:6.0f} {p['active_day_ratio_window']*100:5.0f} "
              f"{p['weather_trades']:6d} {p['weather_pnl']:12,.0f} "
              f"{(p['weather_roi'] or 0)*100:6.1f}% "
              f"{(p['weather_leg_win_rate'] or 0)*100:4.0f}%")

    # Tier A = passes every filter, including the 2-year account-age bar.
    # Tier B = same quality bar, younger account.  The weather category itself is
    # only ~9 months old, so Tier A alone is too thin to diversify across; Tier B
    # rides at half weight.
    def core(p):
        return (
            p["active_day_ratio_window"] >= MIN_ACTIVE_DAY_RATIO
            and p["window_days"] >= 60
            and p["weather_trades"] >= MIN_WEATHER_TRADES
            and p["weather_resolved_legs"] >= MIN_WEATHER_RESOLVED
            and p["weather_pnl"] > MIN_WEATHER_PNL
            and p["days_since_last_trade"] <= 3
        )

    tier_a = [p for p in profiles if core(p) and p["account_age_days"] >= MIN_ACCOUNT_AGE_DAYS]
    tier_b = [p for p in profiles if core(p) and p["account_age_days"] < MIN_ACCOUNT_AGE_DAYS]

    def row(p, tier, weight):
        return {
            "wallet": p["wallet"],
            "name": p["name"] or p["wallet"][:10],
            "weight": weight,
            "_tier": tier,
            "_first_trade": p["first_trade"],
            "_account_age_days": p["account_age_days"],
            "_active_day_ratio": p["active_day_ratio_window"],
            "_window_days": p["window_days"],
            "_weather_trades": p["weather_trades"],
            "_weather_pnl": p["weather_pnl"],
            "_weather_roi": p["weather_roi"],
            "_weather_win_rate": p["weather_leg_win_rate"],
        }

    leaders = ([row(p, "A", 1.0) for p in tier_a]
               + [row(p, "B", 0.5) for p in tier_b[:8]])

    json.dump({
        "generated": datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "min_account_age_days": MIN_ACCOUNT_AGE_DAYS,
            "min_active_day_ratio": MIN_ACTIVE_DAY_RATIO,
            "min_weather_trades": MIN_WEATHER_TRADES,
            "note": ("Tier A meets the 2-year account-age bar; Tier B meets every "
                     "other bar but has a younger account, and rides at half weight."),
        },
        "traders": leaders,
    }, open("data/top_traders.json", "w"), indent=1)
    print(f"\nwrote {len(leaders)} leaders -> data/top_traders.json")


main()
