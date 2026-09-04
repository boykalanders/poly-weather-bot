"""Risk limits, position sizing, and the kill switch.

Every order proposal passes through `RiskManager.check()`.  Nothing else in the
bot is allowed to size a trade.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import settings
from ..store.db import Store

log = logging.getLogger(__name__)

KILL_KEY = "kill_switch"
ARMED_KEY = "armed"
PAUSED_KEY = "paused"


@dataclass
class RiskDecision:
    ok: bool
    size_shares: float = 0.0
    notional: float = 0.0
    reason: str = ""


class RiskManager:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------ switches
    @property
    def killed(self) -> bool:
        return bool(self.store.get_state(KILL_KEY, False))

    @property
    def armed(self) -> bool:
        """Live trading requires an explicit /arm from Telegram each run."""
        return bool(self.store.get_state(ARMED_KEY, False))

    @property
    def paused(self) -> bool:
        return bool(self.store.get_state(PAUSED_KEY, False))

    def set_kill(self, on: bool, reason: str = "") -> None:
        self.store.set_state(KILL_KEY, bool(on))
        if on:
            self.store.set_state("kill_reason", reason)
        log.warning("kill switch %s (%s)", "ENGAGED" if on else "released", reason)

    def set_armed(self, on: bool) -> None:
        self.store.set_state(ARMED_KEY, bool(on))

    def set_paused(self, on: bool) -> None:
        self.store.set_state(PAUSED_KEY, bool(on))

    # ------------------------------------------------------------- sizing
    @staticmethod
    def kelly_size(prob: float, price: float, bankroll: float) -> float:
        """Fractional-Kelly stake in USDC for a binary contract.

        A YES share costs `price` and pays 1.  Net odds b = (1 - price)/price,
        so the Kelly fraction is  f* = (p*(1+b) - 1) / b  =  (p - price)/(1 - price).
        """
        if not (0 < price < 1) or not (0 <= prob <= 1):
            return 0.0
        edge = prob - price
        if edge <= 0:
            return 0.0
        f = edge / (1.0 - price)
        return max(0.0, f * settings.kelly_fraction * bankroll)

    # -------------------------------------------------------------- checks
    def check(
        self, condition_id: str, price: float, prob: float, available_size: float = 1e9
    ) -> RiskDecision:
        if self.killed:
            return RiskDecision(False, reason="kill switch engaged")
        if self.paused:
            return RiskDecision(False, reason="bot paused")
        if settings.trading_mode == "live" and not self.armed:
            return RiskDecision(False, reason="live mode not armed (/arm)")

        today = self.store.today()
        if float(today["realized_pnl"]) <= -abs(settings.max_daily_loss_usdc):
            self.set_kill(True, f"daily loss limit hit ({today['realized_pnl']:.2f} USDC)")
            return RiskDecision(False, reason="daily loss limit")

        remaining_day, remaining_mkt = self.remaining_budget(condition_id)
        if remaining_day <= settings.min_order_usdc:
            return RiskDecision(False, reason="daily notional cap reached")

        if len(self.store.open_positions()) >= settings.max_open_positions:
            return RiskDecision(False, reason="max open positions reached")

        if remaining_mkt <= settings.min_order_usdc:
            return RiskDecision(False, reason="per-market cap reached")

        stake = self.kelly_size(prob, price, settings.bankroll_usdc)
        stake = min(stake, remaining_day, remaining_mkt, settings.max_position_usdc)

        return self.size_order(stake, price, available_size, remaining_day, remaining_mkt)

    # -------------------------------------------------------------- sizing
    def remaining_budget(self, condition_id: str) -> tuple[float, float]:
        """(USDC left today, USDC left in this market) under the configured caps."""
        today = self.store.today()
        remaining_day = settings.max_daily_notional_usdc - float(today["notional"])
        remaining_mkt = settings.max_position_usdc - self.store.position_cost(condition_id)
        return remaining_day, remaining_mkt

    def size_order(
        self,
        stake: float,
        price: float,
        available_size: float = 1e9,
        remaining_day: float | None = None,
        remaining_mkt: float | None = None,
        condition_id: str | None = None,
    ) -> RiskDecision:
        """Turn a target USDC stake into an order, rounded up to the exchange
        minimum.

        A stake below the venue's minimum is not an error, it is dust -- a small
        `COPY_SCALE` against a modest leader trade lands there routinely. Dropping
        those silently means copy trading looks alive while placing nothing, so we
        round the order up to the minimum instead.

        The caps still win: rounding up is refused when the resulting order would
        breach a per-market or daily limit, or exceed the depth on offer. A
        minimum-size order is a floor on what we send, never a licence to exceed a
        risk limit.
        """
        if not (0 < price < 1):
            return RiskDecision(False, reason=f"invalid price {price}")

        if remaining_day is None or remaining_mkt is None:
            # Caller did not pre-compute the budget, so read it now rather than
            # silently sizing against the full caps as if nothing had traded.
            day, mkt = self.remaining_budget(condition_id or "")
            remaining_day = day if remaining_day is None else remaining_day
            remaining_mkt = mkt if remaining_mkt is None else remaining_mkt

        cap = min(remaining_day, remaining_mkt, settings.max_position_usdc)
        # Trim to what the caps allow before sizing, so an oversized request
        # becomes a smaller order rather than no order.
        stake = min(stake, cap)

        shares = stake / price
        # Both floors matter and which one binds depends on the price: at $0.02
        # a share the notional floor needs 50 shares, while at $0.90 the share
        # floor already implies $4.50.
        floor_shares = max(settings.min_order_shares, settings.min_order_usdc / price)
        if shares < floor_shares:
            log.info(
                "rounding dust order up to the exchange minimum: "
                "%.2f sh ($%.2f) -> %.2f sh ($%.2f)",
                shares, stake, floor_shares, floor_shares * price,
            )
            shares = floor_shares
            stake = shares * price     # only recompute when we actually rounded

        if shares > available_size:
            return RiskDecision(
                False,
                reason=f"book too thin: need {shares:.1f} sh, {available_size:.1f} on offer",
            )
        # Only reachable when rounding UP to the venue minimum pushed the order
        # past what the caps allow -- i.e. we cannot trade this market at all
        # without breaching a limit. Tolerance absorbs the few ULPs that
        # shares*price drifts above a cap it exactly equals.
        if stake > cap + 1e-9:
            return RiskDecision(
                False,
                reason=f"minimum order ${stake:.2f} exceeds remaining cap ${cap:.2f}",
            )

        return RiskDecision(True, size_shares=round(shares, 2), notional=round(stake, 2))
