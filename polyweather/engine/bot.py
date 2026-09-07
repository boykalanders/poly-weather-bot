"""The trading engine: wires clients, strategies, risk and execution together.

Runs three background loops
  * copy poll       -- every `copy_poll_interval_sec`
  * settlement      -- every 15 min, books PnL on resolved markets
  * heartbeat       -- daily summary at `heartbeat_hour_utc`

and pushes every fill / error / daily summary out through the notifier.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from ..clients.clob import ClobClient
from ..clients.dataapi import DataClient
from ..clients.gamma import GammaClient
from ..config import settings
from ..store.db import Store
from ..strategy.base import Signal
from ..strategy.copy_trader import CopyTraderStrategy
from .executor import Executor
from .risk import RiskManager

log = logging.getLogger(__name__)


class TradingBot:
    def __init__(self, notify=None):
        self.store = Store(settings.db_path)
        self.gamma = GammaClient()
        self.data = DataClient()
        self.clob = ClobClient()

        self.risk = RiskManager(self.store)
        self.executor = Executor(self.clob, self.store)

        self.copier = CopyTraderStrategy(self.data, self.clob, self.store)

        self._notify = notify or (lambda msg: log.info("NOTIFY: %s", msg))
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.started_at = time.time()
        self.last_poll: float | None = None
        self.last_error: str = ""

    # ------------------------------------------------------------- plumbing
    def set_notifier(self, fn) -> None:
        self._notify = fn

    def notify(self, msg: str) -> None:
        try:
            self._notify(msg)
        except Exception:
            log.exception("notifier failed")

    # -------------------------------------------------------------- signals
    def _handle_signals(self, signals: list[Signal]) -> int:
        placed = 0
        for sig in signals:
            decision = self.risk.check(
                sig.condition_id, sig.price, sig.model_prob, sig.available_size
            )
            if not decision.ok:
                log.debug("skip %s: %s", sig.market, decision.reason)
                continue

            # Copy signals ride the leader's conviction, not our Kelly estimate.
            if sig.strategy == self.copier.name:
                scale = float(sig.meta.get("scale", settings.copy_scale))
                target = float(sig.meta.get("leader_notional", 0)) * scale
                target = min(target, decision.notional, settings.max_position_usdc)
                # Re-size through the risk manager so a scaled-down copy that
                # lands below the venue minimum is rounded up rather than
                # dropped -- and still refused if that breaches a cap.
                decision = self.risk.size_order(
                    target, sig.price, sig.available_size,
                    condition_id=sig.condition_id,
                )
                if not decision.ok:
                    log.info("skip copy %s: %s", sig.market, decision.reason)
                    continue

            ex = self.executor.execute(sig, decision)
            self.notify(ex.summary())
            if ex.ok:
                placed += 1
            else:
                self.last_error = ex.error
        return placed

    # ---------------------------------------------------------------- loops
    def _loop(self, name: str, fn, interval: int) -> None:
        while not self._stop.is_set():
            try:
                fn()
            except Exception as e:
                self.last_error = f"{name}: {e}"
                log.exception("%s loop error", name)
                self.notify(f"⚠️ {name} loop error: {e}")
            self._stop.wait(interval)

    def copy_once(self) -> list[Signal]:
        """One copy-trade pass. Returns the signals it found (pre-risk)."""
        if not settings.enable_copy_trading:
            return []
        signals = self.copier.generate()
        self.last_poll = time.time()
        if signals:
            log.info("copy pass produced %d signal(s)", len(signals))
            self._handle_signals(signals)
        return signals

    def _settlement_pass(self) -> None:
        """Book realized PnL for positions whose market has resolved."""
        for pos in self.store.open_positions():
            market = self.gamma.market_by_condition(pos["condition_id"])
            if not market or not market.closed:
                continue
            # prices settle to exactly [1,0] or [0,1]; index 0 is the YES token
            won = bool(market.prices and market.prices[0] > 0.5)
            payout = float(pos["shares"]) if won else 0.0
            pnl = self.store.resolve_position(pos["token_id"], payout)
            self.notify(
                f"{'🟢 WON' if won else '🔴 LOST'} {pos['market']}\n"
                f"payout ${payout:.2f} | PnL {pnl:+.2f} USDC"
            )

    def _heartbeat_pass(self) -> None:
        """Daily summary, once per UTC day at the configured hour."""
        now = datetime.now(timezone.utc)
        if now.hour != settings.heartbeat_hour_utc:
            return
        today = now.strftime("%Y-%m-%d")
        if self.store.get_state("last_heartbeat") == today:
            return
        self.store.set_state("last_heartbeat", today)
        self.notify("📊 Daily summary\n" + self.status_text())

    # --------------------------------------------------------------- control
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        specs = [
            ("copy", self.copy_once, settings.copy_poll_interval_sec),
            ("settle", self._settlement_pass, 900),
            ("heartbeat", self._heartbeat_pass, 600),
        ]
        for name, fn, interval in specs:
            t = threading.Thread(target=self._loop, args=(name, fn, interval),
                                 name=f"pw-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("engine started (%d loops, mode=%s)", len(self._threads), settings.trading_mode)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()

    # ---------------------------------------------------------------- status
    def status_text(self) -> str:
        today = self.store.today()
        totals = self.store.totals()
        positions = self.store.open_positions()
        uptime = time.time() - self.started_at

        if self.risk.killed:
            state = f"🛑 KILLED ({self.store.get_state('kill_reason', '')})"
        elif self.risk.paused:
            state = "⏸ paused"
        elif settings.trading_mode == "live" and not self.risk.armed:
            state = "🔒 live but NOT armed"
        else:
            state = "🟢 running"

        last = ("never" if not self.last_poll
                else f"{int(time.time() - self.last_poll)}s ago")
        lines = [
            f"Mode: *{settings.trading_mode}*   State: {state}",
            f"Uptime: {uptime / 3600:.1f}h   Last poll: {last}",
            "",
            f"Today: {today['n_trades']} trades, "
            f"${float(today['notional']):.0f} notional, "
            f"PnL {float(today['realized_pnl']):+.2f}",
            f"All time: {totals['n_trades']} trades, PnL {totals['realized_pnl']:+.2f} USDC",
            f"Open positions: {len(positions)}/{settings.max_open_positions}",
            "",
            f"Copy trading: {'on' if settings.enable_copy_trading else 'off'} "
            f"({len(self.copier.leaders)} leaders)",
            f"Limits: ${settings.max_position_usdc:.0f}/mkt, "
            f"${settings.max_daily_notional_usdc:.0f}/day, "
            f"stop -${settings.max_daily_loss_usdc:.0f}",
        ]
        if self.last_error:
            lines += ["", f"Last error: `{self.last_error[:180]}`"]
        return "\n".join(lines)

    def close(self) -> None:
        self.stop()
        for c in (self.gamma, self.data, self.clob):
            try:
                c.close()
            except Exception:
                pass
        self.store.close()
