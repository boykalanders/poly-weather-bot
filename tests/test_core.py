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
