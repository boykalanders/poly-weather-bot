"""Order execution: one code path, two backends (paper simulation / live CLOB)."""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from decimal import Decimal

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


def explain_venue_error(error: str) -> str:
    """Append the setting to change when a rejection is really a config problem.

    The venue's messages are accurate but never name a setting, and each of
    these has cost a round trip to decode from an alert.
    """
    e = (error or "").lower()
    if "maker address not allowed" in e:
        if settings.signature_type != 3:
            hint = ("this account trades from a Deposit Wallet: set SIGNATURE_TYPE=3 "
                    "and FUNDER_ADDRESS to the wallet address in your polymarket.com "
                    "profile menu")
        else:
            hint = ("FUNDER_ADDRESS is not this account's Deposit Wallet: copy the "
                    "wallet address from your polymarket.com profile menu")
    elif "order signer address has to be the address of the api key" in e:
        hint = ("the exchange would not tie this Deposit Wallet order to the key "
                "derived from PRIVATE_KEY -- check PRIVATE_KEY is the key exported "
                "for this account (see py-clob-client-v2 issue #75)")
    elif "not enough balance" in e or "allowance" in e:
        hint = ("the wallet needs pUSD and exchange approvals: a deposit or one trade "
                "on polymarket.com wraps to pUSD and sets them")
    else:
        return error
    return f"{error} -- {hint}"


def quantize_price(price: float, tick: float) -> float:
    """Snap `price` up to the market's tick grid, clamped inside (0, 1).

    Rounding a BUY *up* keeps the limit marketable -- rounding down could land
    below the ask and simply rest on the book instead of filling. The decimal
    places are taken from the tick itself so 0.01 yields 2 dp and 0.0001 four,
    which is what the venue's precision table asks for.
    """
    tick = float(tick or 0.01)
    if tick <= 0:
        tick = 0.01
    steps = math.ceil(round(float(price) / tick, 9))
    decimals = max(0, -Decimal(str(tick)).as_tuple().exponent)
    out = round(steps * tick, decimals)
    # Stay strictly inside the book's bounds: the venue rejects 0 and 1.
    return min(max(out, tick), round(1 - tick, decimals))


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
        # Cross the spread so the limit is marketable, then snap to the market's
        # own price grid: the CLOB rejects any price that is not a multiple of
        # its tick_size, and weather markets quote 0.01 while others go finer.
        # A hardcoded +0.005 produced prices like 0.555 on a 0.01 market, which
        # the venue refuses outright.
        limit = quantize_price(sig.price + sig.tick_size, sig.tick_size)
        notional = round(shares * limit, 2)

        order_id, error, ok = "", "", True
        if mode == "live":
            res = self.clob.place_limit_order(sig.token_id, "BUY", limit, shares, tif="GTC")
            ok, order_id, error = res.ok, res.order_id, explain_venue_error(res.error)
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
