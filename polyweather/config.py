"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- mode ----------
    # paper  = simulate fills locally, never touch the chain
    # live   = place real orders (additionally requires /arm from Telegram)
    trading_mode: Literal["paper", "live"] = "paper"

    # ---------- polymarket ----------
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_host: str = "https://data-api.polymarket.com"
    chain_id: int = 137

    # EOA private key that owns the Polymarket account (live mode only)
    private_key: str = ""
    # Polymarket proxy/funder address (your "deposit address" on the site)
    funder_address: str = ""
    # 0 = EOA, 1 = legacy Proxy Wallet, 2 = legacy Safe Wallet. Polymarket's
    # current Deposit Wallet (type 3) is unsupported -- see the validator below.
    signature_type: int = 1

    # API creds (derived automatically from private_key if left blank)
    clob_api_key: str = ""
    clob_secret: str = ""
    clob_passphrase: str = ""

    # ---------- telegram ----------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""          # primary owner chat
    telegram_allowed_ids: str = ""      # extra comma-separated user ids

    # ---------- strategy toggles ----------
    enable_copy_trading: bool = True

    # ---------- risk ----------
    bankroll_usdc: float = 500.0
    max_position_usdc: float = 25.0          # per market
    max_daily_notional_usdc: float = 200.0   # per UTC day, all markets
    max_open_positions: int = 12
    max_daily_loss_usdc: float = 75.0        # trips the kill switch
    kelly_fraction: float = 0.25             # fractional Kelly

    # Venue minimums. An order sized below these is rounded UP to them rather
    # than dropped -- a small COPY_SCALE against a modest leader trade lands
    # under them routinely, and silently skipping those makes copy trading look
    # alive while placing nothing. Risk caps still override: see
    # RiskManager.size_order.
    min_order_usdc: float = 1.0
    min_order_shares: float = 5.0

    # ---------- copy strategy ----------
    # Leaders to mirror, managed directly in .env.  One entry per wallet,
    # comma separated, each `wallet[:name][:weight]`:
    #   COPY_WALLETS=0xabc…:KickstandBot:1.0, 0xdef…:securebet:0.5
    # Name and weight are optional. When this is empty the bot falls back to
    # `copy_wallets_file`.
    copy_wallets: str = ""
    copy_wallets_file: Path = ROOT / "data" / "top_traders.json"
    copy_scale: float = 0.05         # mirror 5% of the leader's notional
    copy_max_age_sec: int = 900      # ignore trades older than this
    copy_min_leader_notional: float = 50.0
    # Sanity band on the ask we would pay. Outside it we decline to follow the
    # leader: below, the book is dust; above, there is almost no upside left.
    min_price: float = 0.03
    max_price: float = 0.95

    # ---------- loops ----------
    copy_poll_interval_sec: int = 45
    heartbeat_hour_utc: int = 12     # daily summary

    # ---------- storage ----------
    db_path: Path = ROOT / "data" / "polyweather.sqlite3"
    log_level: str = "INFO"

    @field_validator("signature_type")
    @classmethod
    def _known_signature_type(cls, v: int) -> int:
        # py-order-utils only builds orders for 0/1/2 and rejects anything else
        # at signing time. Polymarket now defaults new accounts to a Deposit
        # Wallet (type 3), which this client stack cannot sign for -- catch that
        # here, at startup, rather than when the first armed order is refused.
        if v not in (0, 1, 2):
            raise ValueError(
                f"SIGNATURE_TYPE={v} is not supported by py-clob-client "
                "(0=EOA, 1=proxy/magic, 2=Gnosis safe). Polymarket's Deposit "
                "Wallet is type 3 and needs the newer `polymarket` SDK; this "
                "bot cannot trade live from one."
            )
        return v

    @field_validator("private_key")
    @classmethod
    def _strip_key(cls, v: str) -> str:
        v = (v or "").strip()
        return v if not v or v.startswith("0x") else "0x" + v

    @property
    def allowed_user_ids(self) -> set[int]:
        ids: set[int] = set()
        for raw in (self.telegram_chat_id, self.telegram_allowed_ids):
            for part in str(raw or "").split(","):
                part = part.strip()
                if part.lstrip("-").isdigit():
                    ids.add(int(part))
        return ids

    def require_live_ready(self) -> list[str]:
        """Return a list of missing prerequisites for live trading."""
        missing = []
        if not self.private_key:
            missing.append("PRIVATE_KEY")
        if not self.funder_address:
            missing.append("FUNDER_ADDRESS")
        return missing


settings = Settings()
