"""Tests for the pieces where a silent bug costs money: sizing, risk limits,
leader parsing, and slug matching."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from polyweather.config import settings  # noqa: E402
from polyweather.engine.executor import quantize_price  # noqa: E402
from polyweather.engine.risk import RiskManager  # noqa: E402
from polyweather.store.db import Store  # noqa: E402
from polyweather.strategy.copy_trader import (  # noqa: E402
    is_weather_event, load_leaders, parse_wallet_spec,
)  # noqa: E402



# ------------------------------------------------- weather-market slugs
def test_is_weather_event():
    assert is_weather_event("highest-temperature-in-nyc-on-september-3-2026")
    assert is_weather_event("where-will-it-rain-on-september-3-2026")
    assert not is_weather_event("will-the-fed-cut-rates-in-september")


# ------------------------------------------------- COPY_WALLETS parsing
W = "0xca1f9b9d67d947c8007d9814e8f9d6045cccd282"


def test_wallet_spec_bare_address():
    ld = parse_wallet_spec(W)
    assert ld.wallet == W and ld.weight == 1.0


def test_wallet_spec_name_and_weight():
    ld = parse_wallet_spec(f"{W}:KickstandBot:0.75")
    assert (ld.label, ld.weight) == ("KickstandBot", 0.75)


def test_wallet_spec_weight_without_name():
    """`0xabc:0.5` is a weight, not a wallet nicknamed '0.5'."""
    ld = parse_wallet_spec(f"{W}:0.5")
    assert ld.weight == 0.5 and ld.label == W[:10]


def test_wallet_spec_is_case_insensitive_and_trimmed():
    assert parse_wallet_spec(f"  {W.upper()}  ").wallet == W


def test_wallet_spec_rejects_bad_input():
    assert parse_wallet_spec("0xnothex") is None
    assert parse_wallet_spec("") is None
    assert parse_wallet_spec("   ") is None
    assert parse_wallet_spec(f"{W}:name:-1") is None      # non-positive weight
    assert parse_wallet_spec(f"{W}:name:0") is None
    assert parse_wallet_spec(W[:-1]) is None              # 39 hex chars


def test_env_wallets_parsed_and_deduped(monkeypatch, tmp_path):
    # point the loader away from any real .env so os.environ is authoritative
    monkeypatch.setitem(settings.model_config, "env_file", tmp_path / "absent.env")
    other = "0xaa7a74b8c754e8aacc1ac2dedb699af0a3224d23"
    monkeypatch.setenv("COPY_WALLETS", f"{W}:a:1.0, {other}:b:0.5, {W}:dupe:2.0")
    leaders = load_leaders()
    assert [(l.label, l.weight) for l in leaders] == [("a", 1.0), ("b", 0.5)]


def test_env_wallets_skip_comments_and_blanks(monkeypatch, tmp_path):
    monkeypatch.setitem(settings.model_config, "env_file", tmp_path / "absent.env")
    monkeypatch.setenv("COPY_WALLETS", f"# note,, {W}:solo, ")
    assert [l.label for l in load_leaders()] == ["solo"]


def test_env_wallets_all_invalid_yields_no_leaders(monkeypatch, tmp_path):
    """A typo must not silently fall back to the JSON file -- that would trade
    wallets the operator thought they had removed."""
    monkeypatch.setitem(settings.model_config, "env_file", tmp_path / "absent.env")
    monkeypatch.setenv("COPY_WALLETS", "0xtypo, alsobad")
    assert load_leaders() == []


def test_falls_back_to_json_when_env_empty(monkeypatch, tmp_path):
    monkeypatch.setitem(settings.model_config, "env_file", tmp_path / "absent.env")
    monkeypatch.delenv("COPY_WALLETS", raising=False)
    monkeypatch.setattr(settings, "copy_wallets", "")
    f = tmp_path / "leaders.json"
    f.write_text(json.dumps({"traders": [{"wallet": W, "name": "fromfile"}]}))
    assert [l.label for l in load_leaders(f)] == ["fromfile"]


def test_env_file_beats_process_environment(monkeypatch, tmp_path):
    """Editing .env must take effect on /reload without a restart, so the file
    on disk wins over whatever was loaded into the environment at startup."""
    env = tmp_path / ".env"
    env.write_text(f"COPY_WALLETS={W}:fromfile:1.0\n")
    monkeypatch.setitem(settings.model_config, "env_file", env)
    monkeypatch.setenv("COPY_WALLETS", "0xdeadbeef")   # stale, must be ignored
    assert [l.label for l in load_leaders()] == ["fromfile"]


# -------------------------------------------------------------------- risk
# `settings` is loaded from whatever .env the developer happens to have, so
# without pinning these the suite passes or fails depending on whose machine it
# runs on -- live mode makes the risk checks demand /arm, and a tuned-down
# MAX_POSITION_USDC silently changes what every sizing assertion should expect.
_PINNED = (
    "trading_mode", "bankroll_usdc", "max_position_usdc",
    "max_daily_notional_usdc", "max_open_positions", "max_daily_loss_usdc",
    "kelly_fraction", "min_order_usdc", "min_order_shares",
)


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    """Reset the tuning knobs to their declared defaults for every test."""
    for name in _PINNED:
        monkeypatch.setattr(settings, name, type(settings).model_fields[name].default)


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / "t.sqlite3")
        yield s
        s.close()


def test_kelly_is_zero_without_edge():
    assert RiskManager.kelly_size(prob=0.40, price=0.50, bankroll=1000) == 0.0
    assert RiskManager.kelly_size(prob=0.50, price=0.50, bankroll=1000) == 0.0


def test_kelly_scales_with_edge():
    small = RiskManager.kelly_size(0.55, 0.50, 1000)
    big = RiskManager.kelly_size(0.70, 0.50, 1000)
    assert 0 < small < big


def test_kelly_rejects_degenerate_prices():
    assert RiskManager.kelly_size(0.9, 0.0, 1000) == 0.0
    assert RiskManager.kelly_size(0.9, 1.0, 1000) == 0.0


def test_kill_switch_blocks_everything(store):
    r = RiskManager(store)
    r.set_kill(True, "test")
    assert not r.check("0xabc", price=0.30, prob=0.80).ok


def test_pause_blocks_everything(store):
    r = RiskManager(store)
    r.set_paused(True)
    assert not r.check("0xabc", price=0.30, prob=0.80).ok


def test_healthy_signal_is_sized(store):
    r = RiskManager(store)
    d = r.check("0xabc", price=0.30, prob=0.60)
    assert d.ok and d.size_shares > 0
    assert d.notional <= settings.max_position_usdc


def test_daily_loss_limit_trips_the_kill_switch(store):
    r = RiskManager(store)
    store.upsert_position("tok", 100, 100.0, condition_id="0xdead")
    store.resolve_position("tok", 0.0)          # -100 USDC realized
    assert not r.check("0xabc", price=0.30, prob=0.90).ok
    assert r.killed


def test_per_market_cap_is_enforced(store):
    r = RiskManager(store)
    store.upsert_position("tok", 100, settings.max_position_usdc, condition_id="0xcap")
    assert not r.check("0xcap", price=0.30, prob=0.90).ok


def test_book_depth_limits_size(store):
    r = RiskManager(store)
    assert not r.check("0xabc", price=0.30, prob=0.90, available_size=1.0).ok


# ------------------------------------------- exchange-minimum rounding
def test_dust_order_is_rounded_up_to_the_minimum(store):
    """A tiny COPY_SCALE against a modest leader trade produces sub-$1 dust.
    That must become a minimum-size order, not a silent skip."""
    r = RiskManager(store)
    d = r.size_order(stake=0.05, price=0.20, condition_id="0xabc")
    assert d.ok
    assert d.notional >= settings.min_order_usdc
    assert d.size_shares >= settings.min_order_shares


def test_notional_floor_binds_at_low_prices(store):
    """At $0.02 a share, 5 shares is only $0.10, so the $1 notional floor binds
    and we must buy 50 shares."""
    r = RiskManager(store)
    d = r.size_order(stake=0.01, price=0.02, condition_id="0xabc")
    assert d.ok
    assert d.size_shares == pytest.approx(settings.min_order_usdc / 0.02)
    assert d.notional == pytest.approx(settings.min_order_usdc)


def test_share_floor_binds_at_high_prices(store):
    """At $0.90 a share the $1 notional is met by 1.2 shares, so the 5-share
    floor dominates and the order is $4.50."""
    r = RiskManager(store)
    d = r.size_order(stake=0.10, price=0.90, condition_id="0xabc")
    assert d.ok
    assert d.size_shares == pytest.approx(settings.min_order_shares)
    assert d.notional == pytest.approx(settings.min_order_shares * 0.90)


def test_exact_cap_is_not_rejected_by_float_drift(store):
    """shares*price drifts a few ULPs above a cap it exactly equals; that must
    not reject a legal full-size order."""
    r = RiskManager(store)
    d = r.check("0xabc", price=0.30, prob=0.60)
    assert d.ok and d.notional == pytest.approx(settings.max_position_usdc)


def test_rounding_up_never_breaches_the_per_market_cap(store):
    """Rounding to the minimum is a floor on what we send, not permission to
    exceed a risk limit."""
    r = RiskManager(store)
    d = r.size_order(stake=0.05, price=0.50, remaining_day=100.0, remaining_mkt=0.40)
    assert not d.ok and "cap" in d.reason


def test_rounding_up_never_exceeds_book_depth(store):
    r = RiskManager(store)
    d = r.size_order(stake=0.05, price=0.50, available_size=2.0, condition_id="0xabc")
    assert not d.ok and "thin" in d.reason


def test_size_order_rejects_degenerate_price(store):
    r = RiskManager(store)
    assert not r.size_order(stake=5.0, price=0.0, condition_id="0xabc").ok
    assert not r.size_order(stake=5.0, price=1.0, condition_id="0xabc").ok


def test_size_order_reads_budget_when_not_supplied(store):
    """Called without explicit budgets it must consult the store, not size
    against the full caps as though nothing had traded today."""
    r = RiskManager(store)
    store.record_trade(mode="paper", strategy="s", condition_id="0xabc",
                       notional=settings.max_daily_notional_usdc, side="BUY",
                       price=0.5, size=1)
    assert not r.size_order(stake=5.0, price=0.50, condition_id="0xabc").ok


# ------------------------------------------------------------------- store
def test_resolve_position_books_pnl(store):
    store.upsert_position("tok", 100, 40.0, condition_id="0xa")
    pnl = store.resolve_position("tok", 100.0)
    assert pnl == pytest.approx(60.0)
    assert store.totals()["realized_pnl"] == pytest.approx(60.0)


def test_resolve_is_idempotent(store):
    store.upsert_position("tok", 100, 40.0, condition_id="0xa")
    store.resolve_position("tok", 100.0)
    assert store.resolve_position("tok", 100.0) == 0.0
    assert store.totals()["realized_pnl"] == pytest.approx(60.0)


def test_leader_trade_dedup(store):
    assert store.seen_leader_trade("k1") is False
    assert store.seen_leader_trade("k1") is True


# ------------------------------------------------------- tick-size conformance
def test_quantize_snaps_to_a_penny_market():
    # The CLOB rejects a price that is not a multiple of tick_size. The old
    # fixed +0.005 chase produced 0.555 on a 0.01 market, which is refused.
    assert quantize_price(0.55 + 0.01, 0.01) == 0.56
    assert quantize_price(0.555, 0.01) == 0.56


def test_quantize_rounds_up_so_a_buy_stays_marketable():
    # Rounding down would rest below the ask instead of crossing it.
    assert quantize_price(0.541, 0.01) == 0.55
    assert quantize_price(0.3315, 0.001) == 0.332


def test_quantize_respects_finer_ticks():
    assert quantize_price(0.5505 + 0.001, 0.001) == 0.552
    assert quantize_price(0.12345, 0.0001) == 0.1235


def test_quantize_stays_inside_the_books_bounds():
    assert quantize_price(1.5, 0.01) == 0.99
    assert quantize_price(0.0, 0.01) == 0.01


def test_quantize_survives_a_missing_tick():
    # book() defaults tick_size when the field is absent; never divide by zero.
    assert quantize_price(0.55, 0) == 0.55
    assert quantize_price(0.551, None) == 0.56


def test_every_quantized_price_is_a_tick_multiple():
    for tick in (0.1, 0.01, 0.005, 0.001, 0.0001):
        for raw in (0.013, 0.2222, 0.5, 0.7777, 0.98):
            p = quantize_price(raw, tick)
            assert abs(round(p / tick) - p / tick) < 1e-6, (tick, raw, p)
            assert tick <= p <= 1 - tick


# ------------------------------------------------- caps vs the venue minimum
def _guard_message(monkeypatch, cap):
    """Run the startup guard and return the operator warning, if any."""
    from polyweather.engine.bot import TradingBot
    monkeypatch.setattr(settings, "max_position_usdc", cap)
    bot = TradingBot.__new__(TradingBot)
    sent = []
    bot._notify = sent.append
    bot._warn_if_caps_block_every_order()
    return sent[0] if sent else ""


def test_guard_warns_when_the_cap_cannot_clear_the_venue_floor(monkeypatch):
    # 5 shares at 0.95 costs $4.75, so a $2 per-market cap can never trade.
    msg = _guard_message(monkeypatch, 2.0)
    assert "MAX_POSITION_USDC" in msg and "4.75" in msg


def test_guard_is_quiet_when_the_cap_is_workable(monkeypatch):
    assert _guard_message(monkeypatch, 25.0) == ""


def test_guard_distinguishes_every_market_from_most(monkeypatch):
    # Below the cheapest possible order, nothing can trade at all.
    assert "every market" in _guard_message(monkeypatch, 0.10)
    # Above it but below the dearest, only some markets are blocked.
    assert "most markets" in _guard_message(monkeypatch, 3.0)


# ------------------------------------------------------- wallet types (V2)
def test_every_polymarket_wallet_type_is_accepted():
    # The V2 client signs all four, including the Deposit Wallet (3) that
    # every account created since 2026-05-04 has.
    from polyweather.config import Settings
    for t in (0, 1, 2, 3):
        assert Settings(signature_type=t, _env_file=None).signature_type == t


def test_an_unknown_wallet_type_is_rejected_at_startup():
    from pydantic import ValidationError
    from polyweather.config import Settings
    with pytest.raises(ValidationError):
        Settings(signature_type=4, _env_file=None)


# ------------------------------------------- missing V2 client in live mode
def _hide_v2_client(monkeypatch):
    """Make `import py_clob_client_v2` fail, as in a venv that never got it."""
    import sys
    for name in [m for m in list(sys.modules) if m.startswith("py_clob_client_v2")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", None)


def test_order_failure_names_the_fix_when_the_client_is_missing(monkeypatch):
    # The raw ImportError used to surface first, with no hint at the cause.
    from polyweather.clients.clob import ClobClient
    _hide_v2_client(monkeypatch)
    monkeypatch.setattr(settings, "private_key", "0x" + "1" * 64)
    monkeypatch.setattr(settings, "funder_address", "0x" + "2" * 40)
    c = ClobClient()
    try:
        res = c.place_limit_order("123", "BUY", 0.5, 5.0)
    finally:
        c.close()
    assert not res.ok
    assert "-m pip install -r requirements.txt" in res.error
    assert "not a bare `pip`" in res.error


def test_live_client_check_reports_absence(monkeypatch):
    from polyweather.clients.clob import live_client_installed
    assert live_client_installed()
    _hide_v2_client(monkeypatch)
    assert not live_client_installed()


def _startup_warning(monkeypatch, mode):
    from polyweather.engine.bot import TradingBot
    monkeypatch.setattr(settings, "trading_mode", mode)
    bot = TradingBot.__new__(TradingBot)
    sent = []
    bot._notify = sent.append
    bot._warn_if_live_client_missing()
    return sent[0] if sent else ""


def test_startup_warns_in_live_mode_when_the_client_is_missing(monkeypatch):
    _hide_v2_client(monkeypatch)
    assert "requirements.txt" in _startup_warning(monkeypatch, "live")


def test_startup_is_quiet_in_paper_mode_even_without_the_client(monkeypatch):
    # Paper never touches the CLOB client, so a missing package is harmless.
    _hide_v2_client(monkeypatch)
    assert _startup_warning(monkeypatch, "paper") == ""


def test_startup_is_quiet_when_the_client_is_installed(monkeypatch):
    assert _startup_warning(monkeypatch, "live") == ""


# ------------------------------------------ configured CLOB creds rejected
class _AuthErr(Exception):
    """Stand-in for PolyApiException, which carries the HTTP status."""
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.status_code = status


def _fake_clob(monkeypatch, verdict):
    """Swap in a CLOB client whose get_api_keys answers with `verdict`."""
    import py_clob_client_v2.client as v2

    class Fake:
        def __init__(self, **kw):
            self.creds, self.derived = None, 0

        def set_api_creds(self, creds):
            self.creds = creds

        def get_api_keys(self):
            if isinstance(verdict, BaseException):
                raise verdict
            return ["ok"]

        def create_or_derive_api_key(self):
            self.derived += 1
            return "DERIVED"

    monkeypatch.setattr(v2, "ClobClient", Fake)
    monkeypatch.setattr(settings, "private_key", "0x" + "1" * 64)
    monkeypatch.setattr(settings, "funder_address", "0x" + "2" * 40)
    monkeypatch.setattr(settings, "clob_api_key", "website-key")
    monkeypatch.setattr(settings, "clob_secret", "c2VjcmV0")
    monkeypatch.setattr(settings, "clob_passphrase", "pass")


def _build_inner():
    from polyweather.clients.clob import ClobClient
    c = ClobClient()
    try:
        return c._ensure_client()
    finally:
        c.close()


def test_rejected_configured_creds_fall_back_to_derived(monkeypatch):
    # A Builder/Relayer key pasted into CLOB_API_KEY gets a 401 from the
    # exchange; the bot should derive the right credentials, not fail orders.
    _fake_clob(monkeypatch, _AuthErr(401))
    inner = _build_inner()
    assert inner.derived == 1
    assert inner.creds == "DERIVED"


def test_accepted_configured_creds_are_kept(monkeypatch):
    _fake_clob(monkeypatch, None)
    inner = _build_inner()
    assert inner.derived == 0
    assert inner.creds.api_key == "website-key"


def test_a_network_error_is_not_mistaken_for_bad_creds(monkeypatch):
    # A timeout says nothing about the credentials; re-deriving over a failing
    # link would just fail again, so surface the real error instead.
    _fake_clob(monkeypatch, TimeoutError("slow"))
    with pytest.raises(TimeoutError):
        _build_inner()
