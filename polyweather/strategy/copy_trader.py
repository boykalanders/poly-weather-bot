"""Copy-trading strategy.

Mirrors, at a scaled-down size, the weather-market trades of a configured set of
leader wallets.

Leaders come from `COPY_WALLETS` in .env, falling back to
`data/top_traders.json` when that is empty.  The .env file is re-read on every
load rather than trusted from process start, so /reload picks up an edit without
a restart.

Two hard rules keep this honest:
  * only weather markets are mirrored, even if a leader trades everything else;
  * only trades younger than `copy_max_age_sec` are mirrored -- following a
    leader into a market that has already repriced is how copy bots bleed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from ..clients.clob import ClobClient
from ..clients.dataapi import DataClient, Trade
from ..config import settings
from ..store.db import Store
from .base import Signal, Strategy

log = logging.getLogger(__name__)

# Series/slug fragments that mark an event as a weather market.
WEATHER_HINTS = (
    "temperature", "-rain-", "where-will-it-rain", "daily-weather",
    "snow", "hurricane", "tornado", "heat-", "weather",
)


def is_weather_event(event_slug: str) -> bool:
    s = (event_slug or "").lower()
    return any(h in s for h in WEATHER_HINTS)


@dataclass
class Leader:
    wallet: str
    label: str
    weight: float = 1.0

    @classmethod
    def parse(cls, d: dict) -> "Leader":
        return cls(
            wallet=str(d.get("wallet", "")).lower(),
            label=d.get("name") or d.get("label") or d.get("wallet", "")[:10],
            weight=float(d.get("weight", 1.0)),
        )


_RE_WALLET = re.compile(r"^0x[0-9a-fA-F]{40}$")


def parse_wallet_spec(spec: str) -> Leader | None:
    """Parse one `wallet[:name][:weight]` entry.

    The middle field is optional, so `0xabc:0.5` is read as a weight rather than
    a wallet named "0.5".
    """
    parts = [p.strip() for p in spec.split(":") if p.strip()]
    if not parts:
        return None

    wallet = parts[0].lower()
    if not _RE_WALLET.match(wallet):
        log.error("ignoring leader %r: not a 0x-prefixed 40-hex address", spec)
        return None

    name, weight = "", 1.0
    rest = parts[1:]
    if rest:
        try:                       # `wallet:weight`
            weight = float(rest[-1])
            rest = rest[:-1]
        except ValueError:
            pass
        if rest:
            name = rest[0]
    if weight <= 0:
        log.error("ignoring leader %s: weight %s must be positive", wallet, weight)
        return None

    return Leader(wallet=wallet, label=name or wallet[:10], weight=weight)


def _env_copy_wallets() -> str:
    """Read COPY_WALLETS, preferring the .env file on disk over the value that
    was loaded at process start, so /reload sees edits immediately."""
    env_path = Path(settings.model_config.get("env_file") or "")
    if env_path.is_file():
        value = (dotenv_values(env_path) or {}).get("COPY_WALLETS")
        if value is not None:
            return value
    return os.environ.get("COPY_WALLETS", settings.copy_wallets)


def leader_source_is_env() -> bool:
    """True when COPY_WALLETS is driving the leader list."""
    return bool((_env_copy_wallets() or "").strip())


def load_leaders(path=None) -> list[Leader]:
    """Leaders from COPY_WALLETS, else from the JSON file."""
    raw_spec = (_env_copy_wallets() or "").strip()
    if raw_spec:
        leaders: list[Leader] = []
        seen: set[str] = set()
        for chunk in raw_spec.replace(chr(10), ",").split(","):
            chunk = chunk.strip()
            if not chunk or chunk.startswith("#"):
                continue
            ld = parse_wallet_spec(chunk)
            if ld and ld.wallet not in seen:
                seen.add(ld.wallet)
                leaders.append(ld)
        if leaders:
            log.info("loaded %d leader(s) from COPY_WALLETS", len(leaders))
            return leaders
        log.error("COPY_WALLETS is set but no valid wallet parsed -- copy trading idle")
        return []

    path = path or settings.copy_wallets_file
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("COPY_WALLETS empty and no leader file at %s -- copy trading idle", path)
        return []
    except (ValueError, OSError) as e:
        log.error("cannot read leader file %s: %s", path, e)
        return []

    rows = raw.get("traders", raw) if isinstance(raw, dict) else raw
    leaders = [ld for ld in (Leader.parse(r) for r in rows) if ld.wallet]
    log.info("loaded %d leader(s) from %s", len(leaders), path)
    return leaders


class CopyTraderStrategy(Strategy):
    name = "copy_trader"

    def __init__(self, data: DataClient, clob: ClobClient, store: Store):
        self.data = data
        self.clob = clob
        self.store = store
        self.leaders = load_leaders()
        log.info("copy trading %d leader wallet(s)", len(self.leaders))

    def reload_leaders(self) -> int:
        self.leaders = load_leaders()
        return len(self.leaders)

    # ------------------------------------------------------------------ scan
    def generate(self) -> list[Signal]:
        if not self.leaders:
            return []
        cutoff = int(time.time()) - settings.copy_max_age_sec
        out: list[Signal] = []
        for leader in self.leaders:
            try:
                out += self._scan_leader(leader, cutoff)
            except Exception:
                log.exception("error polling leader %s", leader.wallet)
        return out

    def mirror_trade(self, t: Trade) -> Signal | None:
        """Mirror one trade pushed by the activity stream.

        The same filter chain the poller uses, minus the age cutoff -- a
        streamed fill is seconds old by definition. `seen_leader_trade` keeps
        this idempotent when the backstop poll re-delivers the same trade.
        """
        leader = self._leader_for(t.wallet)
        if leader is None:
            return None
        return self._mirror(leader, t)

    def _leader_for(self, wallet: str) -> Leader | None:
        w = (wallet or "").lower()
        for leader in self.leaders:
            if leader.wallet.lower() == w:
                return leader
        return None

    def _scan_leader(self, leader: Leader, cutoff: int) -> list[Signal]:
        trades = self.data.recent_user_trades(leader.wallet, cutoff)
        out: list[Signal] = []
        for t in trades:
            sig = self._mirror(leader, t)
            if sig:
                out.append(sig)
        return out

    def _mirror(self, leader: Leader, t: Trade) -> Signal | None:
        if t.side.upper() != "BUY":
            return None                     # we only open longs; exits are our own
        if not is_weather_event(t.event_slug):
            return None
        if t.notional < settings.copy_min_leader_notional:
            return None

        key = f"{leader.wallet}:{t.asset}:{t.timestamp}:{t.size}"
        if self.store.seen_leader_trade(key):
            return None

        book = self.clob.book(t.asset)
        if not book or not book.best_ask:
            return None
        ask = book.best_ask
        if not (settings.min_price <= ask <= settings.max_price):
            return None
        # Refuse to chase: if the market moved well past the leader's fill, the
        # information is already in the price.
        if ask > t.price + 0.04:
            log.info("skip copy %s: price moved %.3f -> %.3f", t.title[:40], t.price, ask)
            return None

        # The leader's own fill price is our probability estimate; the "edge" is
        # whatever slippage is left between their entry and the current ask.
        return Signal(
            strategy=self.name,
            event_slug=t.event_slug,
            condition_id=t.condition_id,
            token_id=t.asset,
            market=f"{t.title[:60]} [{t.outcome}]",
            side="BUY",
            price=ask,
            model_prob=min(0.99, max(t.price, ask) + 0.02),
            edge=max(0.0, t.price - ask),
            available_size=book.ask_size,
            tick_size=book.tick_size,
            note=(f"copy {leader.label} ({leader.wallet[:8]}) "
                  f"{t.size:.0f}@{t.price:.3f} = ${t.notional:.0f}"),
            meta={
                "leader": leader.wallet,
                "leader_price": t.price,
                "leader_notional": t.notional,
                "scale": settings.copy_scale * leader.weight,
            },
        )
