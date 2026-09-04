"""Enumerate every Polymarket event tagged `weather` (tag_id=84).

Gamma refuses offsets past ~2.5k, so the query is sliced into 10-day windows
and paginated inside each window.  Output: data/weather_events.json
"""
import collections
import json
import sys
from datetime import date, timedelta

sys.path.insert(0, "research")
from common import GAMMA, get, pmap  # noqa: E402

PAGE = 100
MAX_OFFSET = 2000


def windows(start=date(2023, 1, 1), end=date(2026, 10, 1), step=10):
    d = start
    while d < end:
        n = min(d + timedelta(days=step), end)
        yield d.isoformat() + "T00:00:00Z", n.isoformat() + "T00:00:00Z"
        d = n


def fetch_slice(rng):
    lo, hi = rng
    base = (f"{GAMMA}/events?tag_id=84&limit={PAGE}"
            f"&start_date_min={lo}&start_date_max={hi}&order=startDate&ascending=true")
    out, off = [], 0
    while off <= MAX_OFFSET:
        d = get(f"{base}&offset={off}")
        if not d:
            break
        out += d
        if len(d) < PAGE:
            break
        off += PAGE
    if off > MAX_OFFSET:
        print(f"  WARN window {lo[:10]}..{hi[:10]} hit offset cap", flush=True)
    return out


def main():
    slices = list(windows())
    print(f"{len(slices)} windows", flush=True)
    results = pmap(fetch_slice, slices, workers=8)

    evs, seen = [], set()
    for chunk in results:
        for e in (chunk or []):
            if e["id"] not in seen:
                seen.add(e["id"])
                evs.append(e)
    print("TOTAL events:", len(evs), flush=True)

    out = [{
        "id": e.get("id"), "slug": e.get("slug"), "title": e.get("title"),
        "startDate": e.get("startDate"), "endDate": e.get("endDate"),
        "closed": e.get("closed"), "volume": e.get("volume"),
        "series": [s.get("slug") for s in (e.get("series") or [])],
        "markets": [{
            "c": m.get("conditionId"), "t": m.get("groupItemTitle"),
            "v": m.get("volume"), "op": m.get("outcomePrices"),
            "o": m.get("outcomes"), "closed": m.get("closed"), "slug": m.get("slug"),
        } for m in (e.get("markets") or [])],
    } for e in evs]

    json.dump(out, open("data/weather_events.json", "w"))
    print("markets:", sum(len(e["markets"]) for e in out))
    ser = collections.Counter(s for e in out for s in e["series"])
    print("series:", len(ser))
    for k, v in ser.most_common(20):
        print(f"  {v:5d} {k}")


main()
