"""Polymarket data-api: public trade / position / activity feeds."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import settings
from .http import Http

log = logging.getLogger(__name__)


@dataclass
class Trade:
    wallet: str
    side: str           # BUY / SELL
    asset: str          # clob token id
    condition_id: str
    size: float         # shares
    price: float
    timestamp: int
    title: str
    slug: str
    event_slug: str
    outcome: str
    outcome_index: int
    name: str = ""

    @property
    def notional(self) -> float:
        return self.size * self.price

    @classmethod
    def parse(cls, d: dict) -> "Trade":
        return cls(
            wallet=(d.get("proxyWallet") or "").lower(),
            side=d.get("side") or "",
            asset=str(d.get("asset") or ""),
            condition_id=d.get("conditionId") or "",
            size=float(d.get("size") or 0),
            price=float(d.get("price") or 0),
            timestamp=int(d.get("timestamp") or 0),
            title=d.get("title") or "",
            slug=d.get("slug") or "",
            event_slug=d.get("eventSlug") or "",
            outcome=d.get("outcome") or "",
            outcome_index=int(d.get("outcomeIndex") or 0),
            name=d.get("name") or d.get("pseudonym") or "",
        )


@dataclass
class Position:
    wallet: str
    asset: str
    condition_id: str
    size: float
    avg_price: float
    current_value: float
    cash_pnl: float
    percent_pnl: float
    title: str
    outcome: str
    redeemable: bool

    @classmethod
    def parse(cls, d: dict) -> "Position":
        return cls(
            wallet=(d.get("proxyWallet") or "").lower(),
            asset=str(d.get("asset") or ""),
            condition_id=d.get("conditionId") or "",
            size=float(d.get("size") or 0),
            avg_price=float(d.get("avgPrice") or 0),
            current_value=float(d.get("currentValue") or 0),
            cash_pnl=float(d.get("cashPnl") or 0),
            percent_pnl=float(d.get("percentPnl") or 0),
            title=d.get("title") or "",
            outcome=d.get("outcome") or "",
            redeemable=bool(d.get("redeemable")),
        )


class DataClient:
    """Read-only public data. No auth required."""

    MAX_LIMIT = 500

    def __init__(self):
        self._h = Http(settings.data_host)

    def market_trades(self, condition_id: str, limit: int = 500, offset: int = 0) -> list[Trade]:
        d = self._h.get("/trades", market=condition_id, limit=limit, offset=offset)
        return [Trade.parse(x) for x in (d or [])]

    def user_trades(self, wallet: str, limit: int = 500, offset: int = 0) -> list[Trade]:
        d = self._h.get("/trades", user=wallet, limit=limit, offset=offset)
        return [Trade.parse(x) for x in (d or [])]

    def recent_user_trades(self, wallet: str, since_ts: int) -> list[Trade]:
        """All trades by `wallet` newer than `since_ts` (newest-first feed)."""
        out, off = [], 0
        while off <= 2000:
            batch = self.user_trades(wallet, limit=self.MAX_LIMIT, offset=off)
            if not batch:
                break
            for t in batch:
                if t.timestamp > since_ts:
                    out.append(t)
            if batch[-1].timestamp <= since_ts or len(batch) < self.MAX_LIMIT:
                break
            off += self.MAX_LIMIT
        return out

    def positions(self, wallet: str, limit: int = 500) -> list[Position]:
        d = self._h.get("/positions", user=wallet, limit=limit, sizeThreshold=0.1)
        return [Position.parse(x) for x in (d or [])]

    def portfolio_value(self, wallet: str) -> float:
        d = self._h.get("/value", user=wallet)
        return float(d[0]["value"]) if d else 0.0

    def close(self) -> None:
        self._h.close()
