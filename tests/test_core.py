"""Tests for the pieces where a silent bug costs money: bucket maths, sizing,
risk limits, and slug parsing."""
import json
import math
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from polyweather.config import settings  # noqa: E402
from polyweather.engine.risk import RiskManager  # noqa: E402
from polyweather.store.db import Store  # noqa: E402
from polyweather.strategy.copy_trader import (  # noqa: E402
    is_weather_event, load_leaders, parse_wallet_spec,
)  # noqa: E402
from polyweather.strategy.forecast_edge import (  # noqa: E402
    bucket_of, event_kind, event_target_date, forecast_is_untradeable,
    market_unit,
)
from polyweather.weather.buckets import (  # noqa: E402
    bucket_distribution, bucket_probability, parse_bucket,
)
from polyweather.weather.cities import CITIES, city_from_slug  # noqa: E402
from polyweather.weather.providers import (  # noqa: E402
    DailyEnsemble, ModelConsensus,
)


# ----------------------------------------------------------------- buckets
def test_parse_exact_bucket_is_half_open_around_rounding():
    b = parse_bucket("24°C")
    assert (b.lo, b.hi) == (23.5, 24.5)


def test_parse_open_ended_buckets():
    lo = parse_bucket("23°C or below")
    hi = parse_bucket("33°C or higher")
    assert lo.lo == -math.inf and lo.hi == 23.5
    assert hi.lo == 32.5 and hi.hi == math.inf


def test_parse_range_bucket():
    b = parse_bucket("60-64°F")
    assert (b.lo, b.hi) == (59.5, 64.5)


def test_parse_rejects_non_temperature_labels():
    assert parse_bucket("Yes") is None
    assert parse_bucket("") is None


def _ens(members, unit="C"):
    return DailyEnsemble(city=CITIES["london"], day=date(2026, 9, 5), unit=unit, members=members)


def test_bucket_probability_is_a_probability():
    p = bucket_probability(parse_bucket("20°C"), _ens([19.8, 20.1, 20.4, 21.0]))
    assert 0.0 <= p <= 1.0


def test_ladder_sums_to_one():
    labels = ["18°C or below"] + [f"{t}°C" for t in range(19, 25)] + ["25°C or higher"]
    dist = bucket_distribution(labels, _ens([20.1, 20.4, 21.2, 19.8, 20.9, 22.0]))
    assert sum(dist.values()) == pytest.approx(1.0, abs=1e-9)


def test_kernel_smoothing_keeps_tails_alive():
    """A tight ensemble must not assign literally zero to a neighbouring bucket:
    that is what produces fake 90%+ edges."""
    dist = bucket_distribution(
        ["19°C", "20°C", "21°C"], _ens([20.0, 20.0, 20.0, 20.0])
    )
    assert dist["20°C"] > dist["19°C"] > 0.0


def test_unparseable_bucket_stays_nan_and_does_not_poison_the_ladder():
    dist = bucket_distribution(["20°C", "21°C", "Other"], _ens([20.1, 20.6]))
    assert math.isnan(dist["Other"])
    assert sum(v for v in dist.values() if not math.isnan(v)) == pytest.approx(1.0)


# ------------------------------------------------------------------- slugs
def test_event_target_date():
    assert event_target_date("highest-temperature-in-nyc-on-september-3-2026") == date(2026, 9, 3)
    # Any `-on-<month>-<day>-<year>` slug parses; it is event_kind that decides
    # whether the event belongs to this strategy at all.
    assert event_target_date("where-will-it-rain-on-september-3-2026") == date(2026, 9, 3)
    assert event_target_date("min-arctic-sea-ice-extent-this-summer") is None


def test_event_kind():
    assert event_kind("highest-temperature-in-nyc-on-september-3-2026") == "max"
    assert event_kind("lowest-temperature-in-nyc-on-september-3-2026") == "min"
    assert event_kind("where-will-it-rain-on-september-3-2026") is None


def test_longest_city_key_wins():
    """`la` must not shadow `los-angeles`, nor `panama` shadow `panama-city`."""
    assert city_from_slug("highest-temperature-in-los-angeles-on-september-3-2026").name == "Los Angeles"
    assert city_from_slug("highest-temperature-in-san-francisco-on-may-1-2026").name == "San Francisco"
    assert city_from_slug("highest-temperature-in-panama-city-on-may-1-2026").name == "Panama City"


def test_market_unit_prefers_label_over_city_default():
    assert market_unit(["24°C", "25°C"], CITIES["nyc"]) == "C"
    assert market_unit(["84°F"], CITIES["london"]) == "F"
    assert market_unit(["no unit here"], CITIES["nyc"]) == "F"


# --------------------------------------------- forecast-quality guard
def _mc(**vals):
    return ModelConsensus(vals)


def test_guard_blocks_when_models_genuinely_disagree():
    """Miami 2026-09-06: GFS 95.5F, ECMWF 83.2F, ICON 89.2F, GEM 91.7F.
    12F apart -- no bucket probability from any model is honest."""
    reason = forecast_is_untradeable(
        87.6, 1.97,
        _mc(gfs_seamless=95.5, ecmwf_ifs025=83.2, icon_seamless=89.2, gem_seamless=91.7),
        "F",
    )
    assert reason and "disagree" in reason


def test_guard_blocks_when_our_ensemble_is_the_outlier():
    reason = forecast_is_untradeable(
        24.0, 0.5, _mc(gfs_seamless=26.0, ecmwf_ifs025=26.2, icon_seamless=26.1), "C"
    )
    assert reason and "outlier" in reason


def test_guard_allows_agreeing_models():
    assert forecast_is_untradeable(
        21.7, 0.91, _mc(gfs_seamless=21.6, ecmwf_ifs025=21.9, icon_seamless=21.5), "C"
    ) is None


def test_guard_is_unit_aware():
    """A 2 C spread limit is 3.6 F. The same physical disagreement must not be
    blocked in Celsius and allowed in Fahrenheit."""
    c_spread = _mc(a=20.0, b=22.5)                       # 2.5 C apart -> blocked
    f_spread = _mc(a=68.0, b=72.5)                       # 4.5 F = 2.5 C -> blocked
    assert forecast_is_untradeable(21.2, 0.5, c_spread, "C")
    assert forecast_is_untradeable(70.2, 0.9, f_spread, "F")


def test_guard_fails_open_without_enough_models():
    """A missing cross-check must not silently halt all trading."""
    assert forecast_is_untradeable(28.3, 0.53, ModelConsensus({}), "C") is None
    assert forecast_is_untradeable(28.3, 0.53, _mc(gfs_seamless=30.0), "C") is None


def test_guard_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "model_disagreement_ratio", 0.0)
    assert forecast_is_untradeable(
        28.3, 0.53, _mc(a=40.0, b=41.0, c=42.0), "C"
    ) is None


def test_guard_blocks_bucket_disagreement_inside_the_degree_tolerance():
    """Tel Aviv 2026-09-04: ensemble 32.61 vs model median 32.20 is only 0.41C,
    inside the tolerance -- but they straddle the 32.5 rounding boundary, so we
    say bucket 33 and 3 of 4 models say 32. The market priced 32 at 0.79."""
    labels = ["28°C or below"] + [f"{t}°C" for t in range(29, 38)] + ["38°C or higher"]
    reason = forecast_is_untradeable(
        32.61, 0.65,
        _mc(ecmwf_ifs025=33.3, gem_seamless=31.7, gfs_seamless=32.1, icon_seamless=32.3),
        "C", labels,
    )
    assert reason and "bucket disagreement" in reason


def test_bucket_agreement_is_allowed():
    labels = ["28°C or below"] + [f"{t}°C" for t in range(29, 38)] + ["38°C or higher"]
    assert forecast_is_untradeable(
        32.1, 0.4, _mc(a=32.0, b=32.2, c=31.9, d=32.3), "C", labels
    ) is None


def test_bucket_check_is_skipped_without_labels():
    """Same inputs that the bucket check rejects, minus the ladder: the degree
    checks pass (median 32.4, gap 0.21) so nothing else should fire."""
    mc = _mc(a=32.40, b=32.45, c=32.30)
    labels = ["31°C", "32°C", "33°C"]
    assert forecast_is_untradeable(32.61, 0.65, mc, "C", labels)      # with ladder
    assert forecast_is_untradeable(32.61, 0.65, mc, "C", None) is None  # without


def test_bucket_of_maps_open_ended_labels():
    labels = ["28°C or below", "29°C", "30°C or higher"]
    assert bucket_of(20.0, labels) == "28°C or below"
    assert bucket_of(29.2, labels) == "29°C"
    assert bucket_of(45.0, labels) == "30°C or higher"


def test_consensus_median_and_spread():
    mc = _mc(a=95.5, b=83.2, c=89.2, d=91.7)
    assert mc.spread == pytest.approx(12.3)
    assert mc.median == pytest.approx((89.2 + 91.7) / 2)
    assert ModelConsensus({}).spread == 0.0


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
