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
    event_kind, event_target_date, market_unit,
)
from polyweather.weather.buckets import (  # noqa: E402
    bucket_distribution, bucket_probability, parse_bucket,
)
from polyweather.weather.cities import CITIES, city_from_slug  # noqa: E402
from polyweather.weather.providers import DailyEnsemble  # noqa: E402


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
