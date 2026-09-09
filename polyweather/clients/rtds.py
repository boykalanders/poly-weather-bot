"""Polymarket Real-Time Data Socket: the public activity feed.

The `activity` / `trades` topic streams every fill on Polymarket, each carrying
the trader's `proxyWallet`. That is what copy trading needs, and it is the only
public stream that identifies *who* traded:

  * `wss://ws-subscriptions-clob.polymarket.com/ws/market` carries book and
    price updates for token ids you name -- it never says who traded.
  * `wss://ws-subscriptions-clob.polymarket.com/ws/user` is authenticated and
    scoped to your own account, so it reports your fills, not a leader's.

Server-side filters accept only `event_slug` / `market_slug`, never a wallet,
so we take the firehose and filter locally. It runs around 60 events a second,
which is nothing to match against a handful of addresses.

The payload field names are identical to the data-api `/trades` response, so
`Trade.parse` handles both without a second schema.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Iterable

from .dataapi import Trade

log = logging.getLogger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"

# The server drops a connection that goes quiet, so send an application-level
# ping whenever the socket has been idle this long.
PING_AFTER_SEC = 5.0
# Reconnect backoff: quick at first so a blip costs nothing, capped so a real
# outage does not turn into a hot loop.
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0


class ActivityStream:
    """Background thread streaming trades, filtered to `wallets`.

    `on_trade` is called on the socket thread for every matching fill, so it
    must be quick and thread-safe.
    """

    def __init__(self, on_trade: Callable[[Trade], None],
                 wallets: Iterable[str] | None = None):
        self._on_trade = on_trade
        self.wallets = {w.lower() for w in (wallets or ())}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.last_event_at: float | None = None
        self.events_seen = 0
        self.matches = 0
        self.last_error = ""

    # ---------------------------------------------------------------- control
    def set_wallets(self, wallets: Iterable[str]) -> None:
        """Swap the followed set in place, so /reload needs no reconnect."""
        self.wallets = {w.lower() for w in wallets}

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pw-rtds", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------ guts
    def _run(self) -> None:
        backoff = BACKOFF_START
        while not self._stop.is_set():
            try:
                self._session()
                backoff = BACKOFF_START      # a clean session resets the delay
            except Exception as e:
                self.last_error = str(e)
                log.warning("activity stream disconnected: %s (retry in %.0fs)", e, backoff)
            finally:
                self.connected = False
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, BACKOFF_MAX)

    def _session(self) -> None:
        import websocket        # imported here so the rest of the bot runs without it

        ws = websocket.create_connection(RTDS_URL, timeout=20)
        try:
            ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [{"topic": "activity", "type": "trades"}],
            }))
            self.connected = True
            log.info("activity stream connected, following %d wallet(s)", len(self.wallets))
            ws.settimeout(PING_AFTER_SEC)

            while not self._stop.is_set():
                try:
                    raw = ws.recv()
                except Exception as e:
                    # A read timeout is the idle case: ping and keep waiting.
                    # websocket-client raises its own timeout type, so match on
                    # the name rather than importing the exception hierarchy.
                    if "timeout" in type(e).__name__.lower():
                        ws.send("PING")
                        continue
                    raise
                if not raw:
                    continue
                self._dispatch(raw)
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return                      # PONG and other non-JSON frames
        for ev in (msg if isinstance(msg, list) else [msg]):
            if not isinstance(ev, dict):
                continue
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
            wallet = str(payload.get("proxyWallet") or "").lower()
            if not wallet:
                continue
            self.events_seen += 1
            self.last_event_at = time.time()
            if self.wallets and wallet not in self.wallets:
                continue
            try:
                trade = Trade.parse(payload)
            except (TypeError, ValueError):
                log.debug("unparseable activity payload: %s", payload)
                continue
            self.matches += 1
            try:
                self._on_trade(trade)
            except Exception:
                log.exception("activity stream handler failed")

    # ---------------------------------------------------------------- status
    def status(self) -> str:
        if not self._thread:
            return "off"
        if not self.connected:
            return f"reconnecting ({self.last_error[:60]})" if self.last_error else "connecting"
        age = "" if self.last_event_at is None else f", last event {int(time.time()-self.last_event_at)}s ago"
        return f"live ({self.matches} matched/{self.events_seen} seen{age})"
