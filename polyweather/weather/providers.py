"""Weather forecast providers.

Open-Meteo's ensemble endpoint is the workhorse: it returns one temperature
track per ensemble member, which gives us an empirical predictive distribution
instead of a single deterministic number.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import httpx

from .cities import City

log = logging.getLogger(__name__)

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Multi-model ensemble: GFS (31 members) + ECMWF IFS (51 members).
MODELS = "gfs025,ecmwf_ifs025"

# Four independent deterministic models, fetched in a single request. Used to
# measure how much genuine disagreement there is about a given day.
DETERMINISTIC_MODELS = ("gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_seamless")


@dataclass
class ModelConsensus:
    """Deterministic daily extreme from several independent models."""
    values: dict[str, float]

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def median(self) -> float:
        if not self.values:
            return float("nan")
        v = sorted(self.values.values())
        mid = len(v) // 2
        return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2

    @property
    def spread(self) -> float:
        """Max minus min: how far apart the models are about this day."""
        if self.n < 2:
            return 0.0
        return max(self.values.values()) - min(self.values.values())

    def summary(self) -> str:
        return ", ".join(f"{k.split('_')[0]} {v:.1f}" for k, v in sorted(self.values.items()))


@dataclass
class DailyEnsemble:
    city: City
    day: date
    unit: str                # "F" or "C"
    members: list[float]     # one daily extreme per ensemble member

    @property
    def n(self) -> int:
        return len(self.members)

    @property
    def mean(self) -> float:
        return sum(self.members) / self.n if self.n else float("nan")

    @property
    def spread(self) -> float:
        if self.n < 2:
            return 0.0
        m = self.mean
        return (sum((x - m) ** 2 for x in self.members) / (self.n - 1)) ** 0.5


class OpenMeteoProvider:
    """Ensemble forecasts, cached per city.

    A scan touches ~100 events but only ~50 distinct cities, and the underlying
    models only refresh every few hours, so re-fetching per event would be both
    slow and rude to a free API.  One cached call per city serves every event
    for that city, in both units and for every date in the horizon.
    """

    def __init__(self, timeout: float = 45.0, cache_ttl: float = 1800.0,
                 min_interval: float = 0.6):
        self._c = httpx.Client(timeout=timeout, headers={"User-Agent": "poly-weather-bot/1.0"})
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._det_cache: dict[tuple, tuple[float, dict]] = {}
        self._cache_ttl = cache_ttl
        self._lock = threading.Lock()
        # Pacing gate shared by every thread that touches Open-Meteo.
        self._pace_lock = threading.Lock()
        self._last_request = 0.0
        self._min_interval = min_interval

    def _ensemble_hourly(self, city: City, unit: str, days: int) -> dict:
        key = (city.key, unit, days)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self._cache_ttl:
                return hit[1]

        js = self._fetch_ensemble_hourly(city, unit, days)
        with self._lock:
            self._cache[key] = (now, js)
        return js

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._det_cache.clear()

    def _fetch_ensemble_hourly(self, city: City, unit: str, days: int) -> dict:
        params = {
            "latitude": city.lat,
            "longitude": city.lon,
            "hourly": "temperature_2m",
            "models": MODELS,
            "forecast_days": max(2, min(days, 7)),
            "timezone": city.tz,
            "temperature_unit": "fahrenheit" if unit == "F" else "celsius",
        }
        return self._get_json(ENSEMBLE_URL, params)

    def _get_json(self, url: str, params: dict, tries: int = 4) -> dict:
        """GET with a global pacing gate and back-off on 429.

        Open-Meteo's free tier is generous but per-minute limited, and an
        ensemble call is expensive on their side.  We serialise requests behind
        a minimum interval rather than letting the scan's thread pool stampede.
        """
        last: Exception | None = None
        for attempt in range(tries):
            with self._pace_lock:
                gap = time.monotonic() - self._last_request
                if gap < self._min_interval:
                    time.sleep(self._min_interval - gap)
                self._last_request = time.monotonic()
            try:
                r = self._c.get(url, params=params)
                if r.status_code == 429:
                    raise httpx.HTTPStatusError("429", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                last = e
                if e.response is not None and e.response.status_code == 429:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise
            except httpx.HTTPError as e:
                last = e
                time.sleep(1.0 * (attempt + 1))
        raise last if last else RuntimeError("open-meteo request failed")

    def daily_extremes(
        self, city: City, day: date, unit: str | None = None, kind: str = "max", days: int = 4
    ) -> DailyEnsemble:
        """Per-member daily max (or min) temperature for `day` in the city's local time."""
        unit = unit or city.unit
        js = self._ensemble_hourly(city, unit, days)
        hourly = js.get("hourly") or {}
        times: list[str] = hourly.get("time") or []
        target = day.isoformat()

        # column name -> per-day extreme, keeping every ensemble member separate
        per_member: dict[str, list[float]] = defaultdict(list)
        for key, values in hourly.items():
            if not key.startswith("temperature_2m"):
                continue
            for t, v in zip(times, values):
                if v is not None and t.startswith(target):
                    per_member[key].append(float(v))

        agg = max if kind == "max" else min
        members = [agg(vals) for vals in per_member.values() if vals]
        if not members:
            log.warning("no ensemble data for %s on %s", city.name, target)
        return DailyEnsemble(city=city, day=day, unit=unit, members=members)

    def model_consensus(
        self, city: City, day: date, unit: str | None = None, kind: str = "max"
    ) -> "ModelConsensus":
        """Deterministic runs of four independent forecast models for `day`.

        Deliberately not `best_match`: for US cities Open-Meteo resolves that to
        GFS, so comparing the ensemble against it compares a multi-model
        consensus to a single run that is often the outlier itself.  Miami on
        2026-09-06 was GFS 95.5F, ECMWF 83.2F, ICON 89.2F, GEM 91.7F -- a 12F
        spread in which no single number deserves to be called "the" forecast.

        All four arrive in one request, so this costs one cached call per city.
        """
        unit = unit or city.unit
        field = "temperature_2m_max" if kind == "max" else "temperature_2m_min"
        key = (city.key, unit, field)
        now = time.monotonic()
        with self._lock:
            hit = self._det_cache.get(key)
            js = hit[1] if hit and now - hit[0] < self._cache_ttl else None
        if js is None:
            js = self._get_json(FORECAST_URL, {
                "latitude": city.lat, "longitude": city.lon, "daily": field,
                "timezone": city.tz, "forecast_days": 7,
                "temperature_unit": "fahrenheit" if unit == "F" else "celsius",
                "models": ",".join(DETERMINISTIC_MODELS),
            })
            with self._lock:
                self._det_cache[key] = (now, js)

        d = (js or {}).get("daily") or {}
        try:
            i = d["time"].index(day.isoformat())
        except (KeyError, ValueError):
            return ModelConsensus({})
        values: dict[str, float] = {}
        for model in DETERMINISTIC_MODELS:
            col = d.get(f"{field}_{model}") or []
            if i < len(col) and col[i] is not None:
                values[model] = float(col[i])
        return ModelConsensus(values)

    def precipitation(self, city: City, day: date) -> tuple[float, float]:
        """(mean precipitation-probability %, expected mm) for the local day."""
        params = {
            "latitude": city.lat, "longitude": city.lon,
            "daily": "precipitation_sum,precipitation_probability_max",
            "timezone": city.tz, "forecast_days": 7,
        }
        d = (self._get_json(FORECAST_URL, params) or {}).get("daily") or {}
        try:
            i = d["time"].index(day.isoformat())
        except (KeyError, ValueError):
            return (float("nan"), float("nan"))
        prob = d.get("precipitation_probability_max") or []
        amt = d.get("precipitation_sum") or []
        return (
            float(prob[i]) if i < len(prob) and prob[i] is not None else float("nan"),
            float(amt[i]) if i < len(amt) and amt[i] is not None else float("nan"),
        )

    def close(self) -> None:
        self._c.close()
