"""Order execution: one code path, two backends (paper simulation / live CLOB)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..clients.clob import ClobClient
from ..config import settings
from ..store.db import Store
from ..strategy.base import Signal
from .risk import RiskDecision, RiskManager

log = logging.getLogger(__name__)

# Failures the venue will return for every subsequent order too. Retrying these
# once a scan achieves nothing except a traceback every 300s and a stream of
# identical Telegram alerts -- and, for the geoblock, repeatedly re-requesting
# an endpoint that has already refused us on compliance grounds.
_PERMANENT = (
    "trading restricted in your region",
    "geoblock",
)


def _is_permanent(error: str) -> bool:
    e = (error or "").lower()
    return any(marker in e for marker in _PERMANENT)


@dataclass
class Execution:
    ok: bool
    signal: Signal
    price: float
    shares: float
    notional: float
    mode: str
    order_id: str = ""
    error: str = ""

    def summary(self) -> str:
        head = "✅" if self.ok else "❌"
        tag = "PAPER" if self.mode == "paper" else "LIVE"
        body = (f"{head} [{tag}] {self.signal.strategy}\n"
                f"{self.signal.market}\n"
                f"BUY {self.shares:.1f} @ {self.price:.3f} = ${self.notional:.2f}\n"
                f"model {self.signal.model_prob:.1%} | edge {self.signal.edge:+.1%}")
        if self.error:
            body += f"\nerror: {self.error}"
        return body


class Executor:
    def __init__(self, clob: ClobClient, store: Store):
        self.clob = clob
        self.store = store

    def execute(self, sig: Signal, decision: RiskDecision) -> Execution:
        mode = settings.trading_mode
        shares = decision.size_shares
        # Cross the spread by a tick so marketable limits actually fill.
        limit = min(0.999, round(sig.price + 0.005, 3))
        notional = round(shares * limit, 2)

        order_id, error, ok = "", "", True
        if mode == "live":
            res = self.clob.place_limit_order(sig.token_id, "BUY", limit, shares, tif="GTC")
            ok, order_id, error = res.ok, res.order_id, res.error
        else:
            order_id = f"paper-{int(time.time()*1000)}"

        ex = Execution(
            ok=ok, signal=sig, price=limit, shares=shares,
            notional=notional, mode=mode, order_id=order_id, error=error,
        )

        if ok:
            self.store.record_trade(
                mode=mode, strategy=sig.strategy, event_slug=sig.event_slug,
                condition_id=sig.condition_id, token_id=sig.token_id,
                market=sig.market, side="BUY", price=limit, size=shares,
                notional=notional, model_prob=sig.model_prob, edge=sig.edge,
                order_id=order_id, status="submitted", note=sig.note,
            )
            self.store.upsert_position(
                sig.token_id, shares, notional,
                condition_id=sig.condition_id, event_slug=sig.event_slug, market=sig.market,
            )
            log.info("executed %s %s %.1f@%.3f", mode, sig.market, shares, limit)
        else:
            log.error("execution failed: %s", error)
            if _is_permanent(error):
                # Not a bad order -- the venue is refusing this account outright.
                # Stop the day rather than re-submitting every scan.
                RiskManager(self.store).set_kill(
                    True, "venue refused the order (see logs); trading stopped"
                )
                ex.error = (
                    f"{error} -- this will not succeed on retry, so the kill "
                    "switch is now engaged. Investigate before /revive."
                )

        return ex
