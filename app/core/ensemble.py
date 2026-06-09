"""Ensemble statistics and exceedance probability utilities."""
from __future__ import annotations

import json
from typing import Optional


def percentiles_from_members(members: list[float]) -> dict:
    """Compute p10/p25/p50/p75/p90 from a list of member values."""
    if not members:
        return {}
    s = sorted(members)
    n = len(s)

    def _pct(p: float) -> float:
        idx = p / 100 * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        return round(s[lo] + (s[hi] - s[lo]) * (idx - lo), 3)

    return {
        "precip_p10": _pct(10),
        "precip_p25": _pct(25),
        "precip_p50": _pct(50),
        "precip_p75": _pct(75),
        "precip_p90": _pct(90),
        "precip_mean": round(sum(s) / n, 3),
        "precip_min": round(s[0], 3),
        "precip_max": round(s[-1], 3),
        "ensemble_size": n,
    }


def exceedance_from_members(members: list[float], thresholds: list[float]) -> dict[str, float]:
    """P(X > threshold) for each threshold, exact from member list."""
    n = len(members)
    if not n:
        return {}
    return {
        str(round(t, 6)): round(sum(1 for m in members if m > t) / n, 4)
        for t in thresholds
    }


def exceedance_from_percentiles(
    p10: float, p25: float, p50: float, p75: float, p90: float,
    thresholds: list[float],
) -> dict[str, float]:
    """Approximate P(X > threshold) via linear interpolation of the CDF.

    Known quantile-value pairs: (0.10, p10), (0.25, p25), (0.50, p50),
    (0.75, p75), (0.90, p90).  We interpolate the CDF linearly between
    adjacent knots and clamp outside the range.
    """
    # CDF knots: (value, cumulative_probability)
    knots = sorted([
        (p10, 0.10), (p25, 0.25), (p50, 0.50), (p75, 0.75), (p90, 0.90),
    ])

    result: dict[str, float] = {}
    for t in thresholds:
        # cdf(t) = P(X <= t); exceedance = 1 - cdf(t)
        if t <= knots[0][0]:
            cdf = 0.0  # below p10 → almost all exceed
        elif t >= knots[-1][0]:
            cdf = 1.0  # above p90 → almost none exceed
        else:
            # Find bracketing knots
            for i in range(len(knots) - 1):
                v0, c0 = knots[i]
                v1, c1 = knots[i + 1]
                if v0 <= t <= v1:
                    frac = (t - v0) / (v1 - v0) if v1 != v0 else 0.0
                    cdf = c0 + frac * (c1 - c0)
                    break
        result[str(round(t, 6))] = round(max(0.0, min(1.0, 1.0 - cdf)), 4)
    return result


def compute_exceedance_json(
    thresholds: list[float],
    members: Optional[list[float]] = None,
    p10: Optional[float] = None,
    p25: Optional[float] = None,
    p50: Optional[float] = None,
    p75: Optional[float] = None,
    p90: Optional[float] = None,
) -> Optional[str]:
    """Return exceedance_json string, or None if no ensemble data provided."""
    if not thresholds:
        return None
    if members:
        probs = exceedance_from_members(members, thresholds)
    elif all(x is not None for x in (p10, p25, p50, p75, p90)):
        probs = exceedance_from_percentiles(p10, p25, p50, p75, p90, thresholds)
    else:
        return None
    return json.dumps(probs)


def get_exceedance(exceedance_json: Optional[str], threshold: float) -> Optional[float]:
    """Look up the exceedance probability for a given threshold value."""
    if not exceedance_json:
        return None
    data = json.loads(exceedance_json)
    key = str(round(threshold, 6))
    return data.get(key)
