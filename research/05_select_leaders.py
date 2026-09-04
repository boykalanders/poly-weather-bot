"""Pick the handful of leaders that are actually worth following.

Ranking on lifetime PnL is the wrong test for a copy bot.  What matters is how
many *copyable* signals a wallet produces and whether those particular trades
make money:

  copyable    trades at or above COPY_MIN_LEADER_NOTIONAL.  Most of these
              wallets average $3-$43 a ticket, so a leader can look excellent
              on paper and still never clear the bot's own size filter.
  big_roi     PnL restricted to those copyable trades.  A wallet whose edge
              lives in its dust trades is useless to us even if its headline
              ROI is high.
  sell_share  we mirror BUYs and hold to resolution; heavy sellers diverge.
  per_day     signal rate.  Too few and the leader is idle; too many and we
              are just paying spread on machine churn.

Output: data/leader_shortlist.json
"""
import collections
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, "research")
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402

from common import DATA, get  # noqa: E402

MAX_TRADE_PAGES = 20
COPY_MIN_NOTIONAL = 50.0     # must match settings.copy_min_leader_notional


def load_universe():
    """condition_id -> winning outcome index (None if unresolved)."""
    events = json.load(open("data/weather_events.json"))
    uni = {}
    for e in events:
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
                pr = [float(x) for x in op]
                if max(pr) > 0.99 and min(pr) < 0.01:
                    winner = pr.index(max(pr))
            uni[cid] = winner
    return uni


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


def pnl_of(trades, uni):
    """Realized PnL and win rate over resolved legs built from `trades`."""
    legs = collections.defaultdict(lambda: [0.0, 0.0])
    notional = 0.0
    for t in trades:
        cid = t.get("conditionId")
        size = float(t.get("size") or 0)
        price = float(t.get("price") or 0)
        idx = int(t.get("outcomeIndex") or 0)
        notional += size * price
        leg = legs[(cid, idx)]
        if (t.get("side") or "").upper() == "BUY":
            leg[0] -= size * price
            leg[1] += size
        else:
            leg[0] += size * price
            leg[1] -= size
    pnl = 0.0
    resolved = wins = 0
    for (cid, idx), (cash, shares) in legs.items():
        winner = uni.get(cid)
        if winner is None:
            continue
        resolved += 1
        leg_pnl = cash + shares * (1.0 if idx == winner else 0.0)
        pnl += leg_pnl
        if leg_pnl > 0:
            wins += 1
    return pnl, notional, resolved, (wins / resolved if resolved else None)


def analyse(args):
    wallet, name, uni = args
    trades = [t for t in fetch_trades(wallet) if t.get("conditionId") in uni]
    if not trades:
        return None

    days = {datetime.fromtimestamp(int(t["timestamp"]), tz=timezone.utc).date()
            for t in trades}
    big = [t for t in trades
           if float(t.get("size") or 0) * float(t.get("price") or 0) >= COPY_MIN_NOTIONAL]
    big_buys = [t for t in big if (t.get("side") or "").upper() == "BUY"]

    all_pnl, all_notional, _, all_win = pnl_of(trades, uni)
    big_pnl, big_notional, big_res, big_win = pnl_of(big, uni)

    return {
        "wallet": wallet, "name": name,
        "n_trades": len(trades),
        "n_copyable": len(big),
        "copyable_share": round(len(big) / len(trades), 3),
        "copyable_buys": len(big_buys),
        "copyable_buys_per_day": round(len(big_buys) / max(1, len(days)), 2),
        "active_days": len(days),
        "all_pnl": round(all_pnl, 2),
        "all_roi": round(all_pnl / all_notional, 4) if all_notional else None,
        "all_win_rate": round(all_win, 3) if all_win else None,
        "copyable_pnl": round(big_pnl, 2),
        "copyable_roi": round(big_pnl / big_notional, 4) if big_notional else None,
        "copyable_resolved": big_res,
        "copyable_win_rate": round(big_win, 3) if big_win else None,
        "sell_share": round(sum(1 for t in trades
                                if (t.get("side") or "").upper() == "SELL") / len(trades), 3),
    }


def main():
    uni = load_universe()
    leaders = json.load(open("data/top_traders.json"))["traders"]
    jobs = [(t["wallet"], t["name"], uni) for t in leaders]
    print(f"analysing {len(jobs)} leaders (copy threshold ${COPY_MIN_NOTIONAL:.0f})",
          flush=True)

    rows = []
    with ThreadPoolExecutor(max_workers=9) as ex:
        for fut in as_completed([ex.submit(analyse, j) for j in jobs]):
            r = fut.result()
            if r:
                rows.append(r)

    rows.sort(key=lambda r: -(r["copyable_pnl"]))
    json.dump(rows, open("data/leader_shortlist.json", "w"), indent=1)

    hdr = (f"{'name':20s} {'trades':>7s} {'copyable':>9s} {'%':>5s} {'buys/day':>9s} "
           f"{'copyPnL':>10s} {'copyROI':>8s} {'copyWin':>8s} {'sells':>6s}")
    print("\n" + hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{(r['name'] or '')[:20]:20s} {r['n_trades']:7d} {r['n_copyable']:9d} "
              f"{r['copyable_share']*100:4.0f}% {r['copyable_buys_per_day']:9.2f} "
              f"{r['copyable_pnl']:10,.0f} {(r['copyable_roi'] or 0)*100:7.1f}% "
              f"{(r['copyable_win_rate'] or 0)*100:7.0f}% {r['sell_share']*100:5.0f}%")
    print("\n-> data/leader_shortlist.json")


main()
