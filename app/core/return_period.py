"""Extreme-value return period / return level computation.

Uses GEV (Generalised Extreme Value) distribution fitted to annual maxima
extracted from ObservedRainfall records.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Return periods of interest (years)
STANDARD_RETURN_PERIODS = [2, 5, 10, 25, 50, 100]


def extract_annual_maxima(obs_records: list, variable: str) -> dict[int, float]:
    """Return {year: max_daily_value} from ObservedRainfall records."""
    by_year: dict[int, float] = {}
    for r in obs_records:
        val = getattr(r, variable, None)
        if val is None:
            continue
        y = r.obs_date.year
        if y not in by_year or val > by_year[y]:
            by_year[y] = float(val)
    return by_year


def fit_gev(annual_maxima: list[float]) -> Optional[tuple[float, float, float]]:
    """Fit GEV to annual maxima. Returns (shape, loc, scale) or None."""
    if len(annual_maxima) < 5:
        logger.warning("Too few annual maxima (%d) for GEV fit", len(annual_maxima))
        return None
    try:
        from scipy.stats import genextreme
        c, loc, scale = genextreme.fit(annual_maxima)
        return float(c), float(loc), float(scale)
    except Exception as exc:
        logger.error("GEV fit failed: %s", exc)
        return None


def return_level(shape: float, loc: float, scale: float, return_period: float) -> float:
    """T-year return level from GEV parameters."""
    from scipy.stats import genextreme
    return float(genextreme.ppf(1.0 - 1.0 / return_period, shape, loc=loc, scale=scale))


def return_period_for_value(
    shape: float, loc: float, scale: float, value: float
) -> Optional[float]:
    """Return period in years for a given value (None if effectively infinite)."""
    from scipy.stats import genextreme
    prob_exceed = 1.0 - float(genextreme.cdf(value, shape, loc=loc, scale=scale))
    if prob_exceed <= 1e-6:
        return None  # > 1,000,000-year event — don't display
    return round(1.0 / prob_exceed, 1)


def rp_label(rp: Optional[float]) -> str:
    """Human-readable return period label."""
    if rp is None:
        return ">1000-yr"
    if rp < 2:
        return f"~{rp:.1f}-yr"
    if rp < 10:
        return f"~{rp:.0f}-yr"
    return f"~{rp:.0f}-yr"


def rp_color(rp: Optional[float]) -> str:
    """CSS background color for a return period chip."""
    if rp is None or rp >= 50:
        return "#dc2626"   # red — very rare
    if rp >= 25:
        return "#ea580c"   # orange-red
    if rp >= 10:
        return "#d97706"   # amber
    if rp >= 5:
        return "#ca8a04"   # yellow
    return "#6b7280"       # gray — common event
