"""Gamma API: public market/event metadata and discovery."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import settings
from .http import Http

log = logging.getLogger(__name__)

WEATHER_TAG_ID = 84


def _jloads(v, default):
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


@dataclass
class Market:
    condition_id: str
    question: str
    bucket: str                 # e.g. "24°C", "33°C or higher"
    token_ids: list[str]        # [YES, NO] clob token ids
    outcomes: list[str]
    prices: list[float]
    volume: float
    liquidity: float
    closed: bool
    end_date: str | None
    slug: str = ""

    @property
    def yes_token(self) -> str | None:
        return self.token_ids[0] if self.token_ids else None

    @property
    def yes_price(self) -> float:
        return self.prices[0] if self.prices else 0.0


@dataclass
class WeatherEvent:
    id: str
    slug: str
    title: str
    series: list[str]
    start_date: str | None
    end_date: str | None
    closed: bool
    volume: float
    markets: list[Market] = field(default_factory=list)

    @property
    def end_dt(self) -> datetime | None:
        if not self.end_date:
            return None
        return datetime.fromisoformat(self.end_date.replace("Z", "+00:00"))

    def hours_to_close(self) -> float:
        dt = self.end_dt
        if not dt:
            return 1e9
        return (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0


class GammaClient:
    def __init__(self):
        self._h = Http(settings.gamma_host)

    def _parse_market(self, m: dict) -> Market:
        prices = [float(x) for x in _jloads(m.get("outcomePrices"), [])]
        return Market(
            condition_id=m.get("conditionId") or "",
            question=m.get("question") or "",
            bucket=m.get("groupItemTitle") or "",
            token_ids=[str(t) for t in _jloads(m.get("clobTokenIds"), [])],
            outcomes=_jloads(m.get("outcomes"), []),
            prices=prices,
            volume=float(m.get("volume") or 0),
            liquidity=float(m.get("liquidity") or 0),
            closed=bool(m.get("closed")),
            end_date=m.get("endDate"),
            slug=m.get("slug") or "",
        )

    def _parse_event(self, e: dict) -> WeatherEvent:
        return WeatherEvent(
            id=str(e.get("id")),
            slug=e.get("slug") or "",
            title=e.get("title") or "",
            series=[s.get("slug") for s in (e.get("series") or [])],
            start_date=e.get("startDate"),
            end_date=e.get("endDate"),
            closed=bool(e.get("closed")),
            volume=float(e.get("volume") or 0),
            markets=[self._parse_market(m) for m in (e.get("markets") or [])],
        )

    def open_weather_events(self, limit: int = 100) -> list[WeatherEvent]:
        """Currently tradeable weather events."""
        out, off = [], 0
        while off <= 900:
            d = self._h.get(
                "/events", tag_id=WEATHER_TAG_ID, limit=limit, offset=off,
                closed="false", active="true", order="startDate", ascending="false",
            )
            if not d:
                break
            out += [self._parse_event(e) for e in d]
            if len(d) < limit:
                break
            off += limit
        return out

    def event_by_slug(self, slug: str) -> WeatherEvent | None:
        d = self._h.get("/events", slug=slug)
        return self._parse_event(d[0]) if d else None

    def market_by_condition(self, condition_id: str) -> Market | None:
        d = self._h.get("/markets", condition_ids=condition_id)
        return self._parse_market(d[0]) if d else None

    def close(self) -> None:
        self._h.close()
