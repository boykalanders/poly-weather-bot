"""Forecast-edge strategy.

Build our own probability distribution over tomorrow's high/low temperature
from an 82-member multi-model weather ensemble, compare it against the CLOB's
implied probabilities, and buy whichever bucket the market has underpriced.

This is the strategy with an actual edge story: the market is priced by people
eyeballing a single deterministic forecast, while an ensemble gives a
calibrated distribution.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from ..clients.clob import ClobClient
from ..clients.gamma import GammaClient, WeatherEvent
from ..config import settings
from ..weather.buckets import bucket_distribution
from ..weather.cities import City, city_from_slug
from ..weather.providers import OpenMeteoProvider
from .base import Signal, Strategy

log = logging.getLogger(__name__)

_MONTHS = ("january february march april may june july august september "
           "october november december").split()
_RE_DATE = re.compile(r"-on-(" + "|".join(_MONTHS) + r")-(\d{1,2})-(\d{4})")


def event_target_date(slug: str) -> date | None:
    """`highest-temperature-in-nyc-on-september-3-2026` -> date(2026, 9, 3)."""
    m = _RE_DATE.search(slug or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), _MONTHS.index(m.group(1)) + 1, int(m.group(2)))
    except ValueError:
        return None


def event_kind(slug: str) -> str | None:
    s = (slug or "").lower()
    if "highest-temperature" in s:
        return "max"
    if "lowest-temperature" in s:
        return "min"
    return None


def market_unit(labels: list[str], city: City) -> str:
    """Polymarket quotes US cities in °F and the rest in °C.

    The bucket labels carry the unit explicitly, so prefer them and fall back to
    the city default only when the ladder is unlabelled.
    """
    joined = " ".join(labels)
    if "°F" in joined:
        return "F"
    if "°C" in joined:
        return "C"
    return city.unit


class ForecastEdgeStrategy(Strategy):
    name = "forecast_edge"

    def __init__(self, gamma: GammaClient, clob: ClobClient, wx: OpenMeteoProvider):
        self.gamma = gamma
        self.clob = clob
        self.wx = wx

    # ------------------------------------------------------------------ scan
    def generate(self) -> list[Signal]:
        signals: list[Signal] = []
        try:
            events = self.gamma.open_weather_events()
        except Exception:
            log.exception("failed to list weather events")
            return signals

        # Pre-filter before any network work: most open events are for dates
        # outside the ensemble horizon or in unrecognised cities.
        tradeable = [ev for ev in events if self._is_tradeable(ev)]
        log.info("scanning %d of %d open weather events", len(tradeable), len(events))

        def scan(ev: WeatherEvent) -> list[Signal]:
            try:
                return self._scan_event(ev)
            except Exception:
                log.exception("error scanning %s", ev.slug)
                return []

        # Each event needs one ensemble lookup (usually cached) plus a book
        # fetch per bucket, so the pass is entirely I/O bound.
        with ThreadPoolExecutor(max_workers=4) as pool:
            for chunk in pool.map(scan, tradeable):
                signals += chunk

        signals.sort(key=lambda s: -s.edge)
        return signals

    def _is_tradeable(self, ev: WeatherEvent) -> bool:
        if event_kind(ev.slug) is None:
            return False
        if city_from_slug(ev.slug) is None:
            return False
        target = event_target_date(ev.slug)
        if target is None:
            return False
        hours = ev.hours_to_close()
        if hours < 2 or hours > 24 * 6:
            return False
        if (target - datetime.now(timezone.utc).date()).days > 5:
            return False
        return len([m for m in ev.markets if not m.closed and m.token_ids]) >= 3

    def _scan_event(self, ev: WeatherEvent) -> list[Signal]:
        kind = event_kind(ev.slug)
        if kind is None:
            return []                       # rain//hurricane events: not this strategy
        city = city_from_slug(ev.slug)
        if city is None:
            log.debug("unknown city in %s", ev.slug)
            return []
        target = event_target_date(ev.slug)
        if target is None:
            return []

        markets = [m for m in ev.markets if not m.closed and m.token_ids]
        if len(markets) < 3:
            return []

        labels = [m.bucket for m in markets]
        unit = market_unit(labels, city)
        ens = self.wx.daily_extremes(city, target, unit=unit, kind=kind)
        if ens.n < 10:
            log.warning("thin ensemble for %s (%d members)", ev.slug, ens.n)
            return []

        probs = bucket_distribution(labels, ens)
        out: list[Signal] = []
        for m in markets:
            p = probs.get(m.bucket)
            if p is None or p != p:          # NaN -> unparseable bucket
                continue
            if m.volume < settings.min_market_volume:
                continue

            book = self.clob.book(m.yes_token)
            if not book or not book.best_ask:
                continue
            ask = book.best_ask
            if not (settings.min_price <= ask <= settings.max_price):
                continue
            if book.spread > settings.max_spread:
                continue

            edge = p - ask
            if edge < settings.min_edge:
                continue

            out.append(Signal(
                strategy=self.name,
                event_slug=ev.slug,
                condition_id=m.condition_id,
                token_id=m.yes_token,
                market=f"{city.name} {kind} {target:%b %d} — {m.bucket}",
                side="BUY",
                price=ask,
                model_prob=p,
                edge=edge,
                available_size=book.ask_size,
                note=(f"ens n={ens.n} mean={ens.mean:.1f}{unit} "
                      f"sd={ens.spread:.1f} ask={ask:.3f} model={p:.3f}"),
                meta={"city": city.key, "date": target.isoformat(), "kind": kind},
            ))
        return out
