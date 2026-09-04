"""Turn Polymarket temperature buckets into probabilities from an ensemble."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .providers import DailyEnsemble

# "23°C or below" / "84°F or above" / "29°C" / "12 or below"
_NUM = r"(-?\d+(?:\.\d+)?)"
_RE_BELOW = re.compile(rf"{_NUM}\s*°?[CF]?\s*(?:or\s+)?(?:below|less|lower|under|and below)", re.I)
_RE_ABOVE = re.compile(rf"{_NUM}\s*°?[CF]?\s*(?:or\s+)?(?:above|higher|more|greater|over|and above)", re.I)
_RE_RANGE = re.compile(rf"{_NUM}\s*°?[CF]?\s*(?:-|–|to)\s*{_NUM}", re.I)
_RE_EXACT = re.compile(rf"^\s*{_NUM}\s*°?[CF]?\s*$", re.I)


@dataclass(frozen=True)
class Bucket:
    """Half-open interval [lo, hi) on the *rounded-to-integer* temperature."""
    label: str
    lo: float
    hi: float

    @property
    def is_open_low(self) -> bool:
        return self.lo == -math.inf

    @property
    def is_open_high(self) -> bool:
        return self.hi == math.inf


def parse_bucket(label: str) -> Bucket | None:
    """Parse a Polymarket `groupItemTitle` into a temperature interval.

    Polymarket resolves on the integer reported high, so "24°C" means the
    reported high rounds to 24, i.e. the real value lies in [23.5, 24.5).
    """
    s = (label or "").strip()
    if not s:
        return None

    if m := _RE_BELOW.search(s):
        v = float(m.group(1))
        return Bucket(s, -math.inf, v + 0.5)
    if m := _RE_ABOVE.search(s):
        v = float(m.group(1))
        return Bucket(s, v - 0.5, math.inf)
    if m := _RE_RANGE.search(s):
        a, b = float(m.group(1)), float(m.group(2))
        return Bucket(s, min(a, b) - 0.5, max(a, b) + 0.5)
    if m := _RE_EXACT.match(s):
        v = float(m.group(1))
        return Bucket(s, v - 0.5, v + 0.5)
    return None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_probability(bucket: Bucket, ens: DailyEnsemble, kernel_sigma: float | None = None) -> float:
    """P(temperature falls in `bucket`) under a kernel-smoothed ensemble.

    Raw ensembles are under-dispersed: members cluster tighter than reality, so
    counting members per bucket produces over-confident probabilities.  We
    therefore place a Gaussian kernel on each member and average the resulting
    CDFs.  Bandwidth defaults to a floor plus a fraction of ensemble spread.
    """
    if not ens.members:
        return float("nan")

    sigma = kernel_sigma if kernel_sigma is not None else default_bandwidth(ens)
    sigma = max(sigma, 1e-6)

    total = 0.0
    for m in ens.members:
        hi = 1.0 if bucket.is_open_high else _norm_cdf((bucket.hi - m) / sigma)
        lo = 0.0 if bucket.is_open_low else _norm_cdf((bucket.lo - m) / sigma)
        total += max(0.0, hi - lo)
    return total / len(ens.members)


def default_bandwidth(ens: DailyEnsemble) -> float:
    """Bandwidth in the ensemble's own unit.

    Floor reflects irreducible station/rounding noise (~0.8°F, ~0.45°C); the
    spread term widens the distribution when the models themselves disagree.
    """
    floor = 0.8 if ens.unit == "F" else 0.45
    return max(floor, 0.35 * ens.spread)


def bucket_distribution(
    labels: list[str], ens: DailyEnsemble, kernel_sigma: float | None = None
) -> dict[str, float]:
    """Probabilities for a full market ladder, renormalised to sum to 1.

    Renormalisation matters: the ladder is exhaustive and mutually exclusive by
    construction, so any leftover mass is a modelling artefact.
    """
    raw: dict[str, float] = {}
    for label in labels:
        b = parse_bucket(label)
        raw[label] = bucket_probability(b, ens, kernel_sigma) if b else float("nan")

    valid = {k: v for k, v in raw.items() if not math.isnan(v)}
    total = sum(valid.values())
    if total <= 0:
        return raw
    return {k: (v / total if k in valid else v) for k, v in raw.items()}
