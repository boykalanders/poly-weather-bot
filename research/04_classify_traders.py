"""Remove institution-style traders from the leader set.

Two separate questions, deliberately kept apart:

1. **Is this an institution?**  Measured by scale -- average ticket and total
   weather notional.  In the profiled data these separate cleanly: tickets run
   $4,445 / $744 / $492 and then fall off a cliff to $77, and the same three
   wallets are the only ones above $1M of weather notional.  Those are desks,
   not individuals, and they are removed outright.

2. **Does the wallet's style match how we copy it?**  `copy_trader` mirrors BUY
   legs and holds to resolution.  A wallet that sells out of a large share of
   its positions is not doing that, so mirroring its buys misrepresents its
   strategy.  Rather than discard those wallets, they ride at half weight --
   they still carry information, just less of it than a buy-and-hold forecaster.

Reads data/trader_style.json if present (microstructure is expensive to fetch),
otherwise rebuilds it.  Rewrites data/top_traders.json.
"""
import collections
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "research")
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402

from common import DATA, get  # noqa: E402

MAX_TRADE_PAGES = 20
STYLE_CACHE = "data/trader_style.json"

# --- institution-style: scale. Either test is sufficient. -------------------
INSTITUTIONAL_TICKET_USDC = 200.0        # average ticket
INSTITUTIONAL_NOTIONAL_USDC = 1_000_000.0  # total weather notional

# --- strategy fit: we hold to resolution, so heavy sellers are down-weighted -
HIGH_SELL_SHARE = 0.40
HIGH_SELL_WEIGHT_MULTIPLIER = 0.5


def load_universe():
    events = json.load(open("data/weather_events.json"))
    return {m["c"] for e in events for m in e.get("markets", []) if m.get("c")}


def fetch_trades(wallet):
    out = []
    for page in range(MAX_TRADE_PAGES):
        d = get(f"{DATA}/trades?user={wallet}&limit=500&offset={page * 500}")
        if not d:
            break
        out += d
        if len(d) < 500:
            break
    return out


def style(args):
    wallet, name, universe = args
    trades = [t for t in fetch_trades(wallet) if t.get("conditionId") in universe]
    if len(trades) < 50:
        return None

    sells = 0
    notional = 0.0
    legs = collections.Counter()
    days = set()
    for t in trades:
        size = float(t.get("size") or 0)
        price = float(t.get("price") or 0)
        notional += size * price
        if (t.get("side") or "").upper() == "SELL":
            sells += 1
        legs[(t.get("conditionId"), int(t.get("outcomeIndex") or 0))] += 1
        days.add(datetime.fromtimestamp(int(t["timestamp"]), tz=timezone.utc).date())

    n = len(trades)
    return {
        "wallet": wallet, "name": name, "weather_trades": n,
        "sell_share": round(sells / n, 3),
        "trades_per_leg": round(n / max(1, len(legs)), 2),
        "trades_per_day": round(n / max(1, len(days)), 1),
        "avg_trade_usdc": round(notional / n, 2),
        "distinct_legs": len(legs), "active_days": len(days),
    }


def get_styles(wallets, profiles):
    cached = {}
    if os.path.exists(STYLE_CACHE):
        cached = {s["wallet"]: s for s in json.load(open(STYLE_CACHE))}
    missing = [w for w in wallets if w not in cached]
    if missing:
        print(f"fetching microstructure for {len(missing)} wallet(s)", flush=True)
        universe = load_universe()
        jobs = [(w, profiles[w]["name"], universe) for w in missing if w in profiles]
        with ThreadPoolExecutor(max_workers=10) as ex:
            for fut in as_completed([ex.submit(style, j) for j in jobs]):
                s = fut.result()
                if s:
                    cached[s["wallet"]] = s
        json.dump(list(cached.values()), open(STYLE_CACHE, "w"), indent=1)
    else:
        print("using cached microstructure", flush=True)
    return cached


def main():
    profiles = {p["wallet"]: p for p in json.load(open("data/trader_profiles.json"))}
    leaders = json.load(open("data/top_traders.json"))
    wallets = [t["wallet"] for t in leaders["traders"]]
    styles = get_styles(wallets, profiles)

    hdr = (f"{'name':20s} {'notional':>12s} {'ticket':>8s} {'sells':>6s} "
           f"{'roi':>7s} {'w':>5s}  verdict")
    print("\n" + hdr)
    print("-" * len(hdr))

    keep, dropped = [], []
    for t in sorted(leaders["traders"],
                    key=lambda r: -profiles[r["wallet"]]["weather_notional"]):
        w = t["wallet"]
        s, p = styles.get(w), profiles[w]
        if not s:
            continue
        notional = p["weather_notional"]
        ticket = s["avg_trade_usdc"]

        reasons = []
        if ticket >= INSTITUTIONAL_TICKET_USDC:
            reasons.append(f"ticket ${ticket:,.0f}")
        if notional >= INSTITUTIONAL_NOTIONAL_USDC:
            reasons.append(f"notional ${notional/1e6:.1f}M")

        row = dict(t)
        row.update({
            "_sell_share": s["sell_share"],
            "_trades_per_leg": s["trades_per_leg"],
            "_trades_per_day": s["trades_per_day"],
            "_avg_trade_usdc": ticket,
        })

        if reasons:
            verdict = "DROP institutional: " + ", ".join(reasons)
            weight = 0.0
            dropped.append((t["name"], reasons))
        else:
            weight = t["weight"]
            note = ""
            if s["sell_share"] > HIGH_SELL_SHARE:
                weight = round(weight * HIGH_SELL_WEIGHT_MULTIPLIER, 3)
                note = f" (halved: sells {s['sell_share']:.0%}, does not hold to resolution)"
            row["weight"] = weight
            keep.append(row)
            verdict = "keep" + note

        print(f"{(t['name'] or '')[:20]:20s} ${notional:11,.0f} ${ticket:7,.0f} "
              f"{s['sell_share']*100:5.0f}% {(p['weather_roi'] or 0)*100:6.1f}% "
              f"{weight:5.2f}  {verdict}")

    leaders["traders"] = keep
    leaders["criteria"]["style_filter"] = {
        "institutional_ticket_usdc": INSTITUTIONAL_TICKET_USDC,
        "institutional_notional_usdc": INSTITUTIONAL_NOTIONAL_USDC,
        "high_sell_share": HIGH_SELL_SHARE,
        "high_sell_weight_multiplier": HIGH_SELL_WEIGHT_MULTIPLIER,
        "note": ("Institution-style wallets (desk-scale tickets or >$1M weather "
                 "notional) are removed. copy_trader mirrors BUYs and holds to "
                 "resolution, so wallets that sell out of most positions ride at "
                 "half weight rather than being treated as buy-and-hold."),
        "removed": [{"name": n, "reasons": r} for n, r in dropped],
    }
    json.dump(leaders, open("data/top_traders.json", "w"), indent=1)
    print(f"\nkept {len(keep)} of {len(wallets)} leaders "
          f"({len(dropped)} institution-style removed) -> data/top_traders.json")


main()
