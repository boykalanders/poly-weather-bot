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

        remaining_day = settings.max_daily_notional_usdc - float(today["notional"])
        if remaining_day <= 1.0:
            return RiskDecision(False, reason="daily notional cap reached")

        if len(self.store.open_positions()) >= settings.max_open_positions:
            return RiskDecision(False, reason="max open positions reached")

        already = self.store.position_cost(condition_id)
        remaining_mkt = settings.max_position_usdc - already
        if remaining_mkt <= 1.0:
            return RiskDecision(False, reason="per-market cap reached")

        stake = self.kelly_size(prob, price, settings.bankroll_usdc)
        stake = min(stake, remaining_day, remaining_mkt, settings.max_position_usdc)
        if stake < 1.0:
            return RiskDecision(False, reason=f"stake too small ({stake:.2f})")

        shares = stake / price
        if shares > available_size:
            shares = available_size
            stake = shares * price
        if stake < 1.0 or shares < 5.0:
            return RiskDecision(False, reason="insufficient book depth")

        return RiskDecision(True, size_shares=round(shares, 2), notional=round(stake, 2))
