"""CLOB client: order books (public) and order placement (authenticated).

`py_clob_client_v2` is imported lazily so that paper mode — and the Telegram
control surface — keep working on a machine with no wallet configured.

This has to be the V2 client. The CLOB moved to CTF Exchange V2 and dropped V1
outright on 2026-04-28: the order struct lost `nonce` / `feeRateBps` and gained
`timestamp` / `metadata` / `builder`, the exchange contracts changed, and pUSD
replaced USDC.e as collateral. The legacy `py-clob-client` still signs the old
struct, which the venue refuses with "invalid order version". The V2 client asks
the server for the current order version and re-signs on a mismatch, so the
next version bump should not strand the bot the same way.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import settings
from .http import Http

log = logging.getLogger(__name__)


@dataclass
class Book:
    token_id: str
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    # Per-market, from the same /book response. The CLOB rejects an order whose
    # price is not a multiple of tick_size, so a fixed rounding rule is not safe
    # -- weather markets quote 0.01 while others go to 0.0001.
    tick_size: float = 0.01
    min_order_size: float = 0.0

    @property
    def mid(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid or self.best_ask or 0.0

    @property
    def spread(self) -> float:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return 1.0


@dataclass
class OrderResult:
    ok: bool
    order_id: str = ""
    status: str = ""
    error: str = ""
    filled_size: float = 0.0
    avg_price: float = 0.0


class ClobClient:
    def __init__(self):
        self._h = Http(settings.clob_host)
        self._client = None      # py_clob_client_v2 instance, built on demand
        self._auth_error: str | None = None

    # ------------------------------------------------------------------ public
    def book(self, token_id: str) -> Book | None:
        d = self._h.get("/book", token_id=token_id)
        if not d:
            return None
        bids = d.get("bids") or []
        asks = d.get("asks") or []
        # The API returns bids ascending and asks descending by price.
        best_bid = max((float(b["price"]) for b in bids), default=0.0)
        best_ask = min((float(a["price"]) for a in asks), default=0.0)
        bid_sz = sum(float(b["size"]) for b in bids if float(b["price"]) == best_bid)
        ask_sz = sum(float(a["size"]) for a in asks if float(a["price"]) == best_ask)
        return Book(
            token_id, best_bid, best_ask, bid_sz, ask_sz,
            tick_size=float(d.get("tick_size") or 0.01),
            min_order_size=float(d.get("min_order_size") or 0.0),
        )

    def midpoint(self, token_id: str) -> float | None:
        d = self._h.get("/midpoint", token_id=token_id)
        return float(d["mid"]) if d and "mid" in d else None

    # -------------------------------------------------------------- authenticated
    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if self._auth_error:
            raise RuntimeError(self._auth_error)

        missing = settings.require_live_ready()
        if missing:
            self._auth_error = f"missing config: {', '.join(missing)}"
            raise RuntimeError(self._auth_error)

        try:
            from py_clob_client_v2.client import ClobClient as _Clob
            from py_clob_client_v2.clob_types import ApiCreds
        except ImportError as e:  # pragma: no cover
            self._auth_error = (
                f"py-clob-client-v2 not installed ({e}) -- run "
                "`pip install -r requirements.txt`"
            )
            raise RuntimeError(self._auth_error) from e

        c = _Clob(
            host=settings.clob_host,
            key=settings.private_key,
            chain_id=settings.chain_id,
            signature_type=settings.signature_type,
            funder=settings.funder_address,
        )
        if settings.clob_api_key and settings.clob_secret and settings.clob_passphrase:
            creds = ApiCreds(
                api_key=settings.clob_api_key,
                api_secret=settings.clob_secret,
                api_passphrase=settings.clob_passphrase,
            )
        else:
            # Deterministically derive (or create) L2 creds from the signing key.
            # V2 renamed this from create_or_derive_api_creds.
            creds = c.create_or_derive_api_key()
        c.set_api_creds(creds)
        self._client = c
        log.info("CLOB authenticated for funder %s", settings.funder_address)
        return c

    def place_limit_order(
        self, token_id: str, side: str, price: float, size: float, tif: str = "GTC"
    ) -> OrderResult:
        """Place a limit order. `size` is in shares, `price` in USDC per share."""
        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            from py_clob_client_v2.order_builder.constants import BUY, SELL

            c = self._ensure_client()
            args = OrderArgs(
                token_id=token_id,
                # The executor has already snapped this to the market's tick
                # grid. Rounding to 3 dp here would silently move a price on a
                # 0.0001-tick market; 6 dp only strips float noise.
                price=round(float(price), 6),
                size=round(float(size), 2),
                side=BUY if side.upper() == "BUY" else SELL,
            )
            signed = c.create_order(args)
            order_type = getattr(OrderType, tif, OrderType.GTC)
            resp = c.post_order(signed, order_type)
            if not isinstance(resp, dict):
                resp = {"success": bool(resp), "status": str(resp)}
            ok = bool(resp.get("success", True)) and not resp.get("errorMsg")
            return OrderResult(
                ok=ok,
                order_id=str(resp.get("orderID") or resp.get("orderId") or ""),
                status=str(resp.get("status") or ""),
                error=str(resp.get("errorMsg") or ""),
            )
        except Exception as e:
            log.exception("order placement failed")
            return OrderResult(ok=False, error=str(e))

    def cancel_all(self) -> OrderResult:
        try:
            c = self._ensure_client()
            resp = c.cancel_all()
            return OrderResult(ok=True, status=str(resp))
        except Exception as e:
            return OrderResult(ok=False, error=str(e))

    def open_orders(self) -> list[dict]:
        try:
            # V2 replaced get_orders with the paginated get_open_orders.
            return self._ensure_client().get_open_orders() or []
        except Exception as e:
            log.warning("open_orders failed: %s", e)
            return []

    def health(self) -> str:
        try:
            self._ensure_client()
            return "authenticated"
        except Exception as e:
            return f"unauthenticated ({e})"

    def close(self) -> None:
        self._h.close()
