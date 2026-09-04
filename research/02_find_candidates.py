"""Discover candidate wallets active in Polymarket weather markets.

Samples the highest-volume weather markets across the whole history of the
category and collects every wallet that traded them.  Output:
data/candidates.json  (wallet -> markets touched, notional, first/last seen)
"""
import collections
import json
import random
import sys

sys.path.insert(0, "research")
from common import DATA, get, pmap  # noqa: E402

SAMPLE_PER_MONTH = 90       # markets sampled from each calendar month
TRADE_PAGES = 3             # 500 trades per page
MIN_MARKET_VOLUME = 500.0


def load_markets():
    events = json.load(open("data/weather_events.json"))
    rows = []
    for e in events:
        month = (e.get("startDate") or "?")[:7]
        for m in e.get("markets", []):
            if not m.get("c"):
                continue
            if float(m.get("v") or 0) < MIN_MARKET_VOLUME:
                continue
            rows.append({
                "condition_id": m["c"], "event_slug": e["slug"], "month": month,
                "volume": float(m.get("v") or 0), "bucket": m.get("t"),
            })
    return rows


def sample(rows):
    """Volume-weighted-ish sample, balanced across months so early months of the
    category are not swamped by the 2026 explosion in market count."""
    by_month = collections.defaultdict(list)
    for r in rows:
        by_month[r["month"]].append(r)
    out = []
    for month, group in sorted(by_month.items()):
        group.sort(key=lambda r: -r["volume"])
        head = group[:SAMPLE_PER_MONTH // 2]
        tail = group[SAMPLE_PER_MONTH // 2:]
        random.shuffle(tail)
        out += head + tail[:SAMPLE_PER_MONTH - len(head)]
    return out


def fetch_trades(row):
    out = []
    for page in range(TRADE_PAGES):
        d = get(f"{DATA}/trades?market={row['condition_id']}&limit=500&offset={page * 500}")
        if not d:
            break
        out += d
        if len(d) < 500:
            break
    return (row, out)


def main():
    random.seed(7)
    rows = load_markets()
    print(f"{len(rows)} weather markets above ${MIN_MARKET_VOLUME:.0f} volume")
    picked = sample(rows)
    print(f"sampling {len(picked)} markets", flush=True)

    wallets = collections.defaultdict(lambda: {
        "wallet": "", "name": "", "markets": set(), "events": set(),
        "n_trades": 0, "notional": 0.0, "first_ts": 10**11, "last_ts": 0,
    })

    done = 0
    for res in pmap(fetch_trades, picked, workers=10):
        done += 1
        if done % 200 == 0:
            print(f"  {done}/{len(picked)} markets, {len(wallets)} wallets", flush=True)
        if not res:
            continue
        row, trades = res
        for t in trades:
            w = (t.get("proxyWallet") or "").lower()
            if not w:
                continue
            rec = wallets[w]
            rec["wallet"] = w
            rec["name"] = rec["name"] or t.get("name") or t.get("pseudonym") or ""
            rec["markets"].add(row["condition_id"])
            rec["events"].add(row["event_slug"])
            rec["n_trades"] += 1
            rec["notional"] += float(t.get("size") or 0) * float(t.get("price") or 0)
            ts = int(t.get("timestamp") or 0)
            rec["first_ts"] = min(rec["first_ts"], ts)
            rec["last_ts"] = max(rec["last_ts"], ts)

    out = []
    for rec in wallets.values():
        out.append({
            "wallet": rec["wallet"], "name": rec["name"],
            "n_markets": len(rec["markets"]), "n_events": len(rec["events"]),
            "n_trades_sampled": rec["n_trades"],
            "notional_sampled": round(rec["notional"], 2),
            "first_ts": rec["first_ts"], "last_ts": rec["last_ts"],
        })
    out.sort(key=lambda r: -r["notional_sampled"])
    json.dump(out, open("data/candidates.json", "w"), indent=1)

    print(f"\nTOTAL wallets seen: {len(out)}")
    print(f"  >=10 weather markets: {sum(1 for r in out if r['n_markets'] >= 10)}")
    print(f"  >=50 weather markets: {sum(1 for r in out if r['n_markets'] >= 50)}")
    print("\nTop 25 by sampled weather notional:")
    for r in out[:25]:
        print(f"  {r['notional_sampled']:12,.0f}  mkts={r['n_markets']:4d}  "
              f"{r['wallet']}  {r['name'][:24]}")


main()
