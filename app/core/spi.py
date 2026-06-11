"""
Standardized Precipitation Index (SPI) computation.
WMO-recommended gamma distribution method (McKee et al., 1993).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)

TIMESCALES = [1, 3, 6]

# (lower_bound_inclusive, label, hex_colour)
_CATEGORIES: list[tuple[float | None, str, str]] = [
    (2.00,  "Extremely Wet",  "#1a65b0"),
    (1.50,  "Severely Wet",   "#4da1d4"),
    (1.00,  "Moderately Wet", "#a8d4ea"),
    (-1.00, "Near Normal",    "#d1d5db"),
    (-1.50, "Moderately Dry", "#fde68a"),
    (-2.00, "Severely Dry",   "#f97316"),
    (None,  "Extremely Dry",  "#991b1b"),
]


def spi_category(value: float | None) -> tuple[str, str]:
    """Return (label, colour) for a given SPI value."""
    if value is None:
        return "Insufficient data", "#e5e7eb"
    for threshold, label, colour in _CATEGORIES:
        if threshold is None or value >= threshold:
            return label, colour
    return "Extremely Dry", "#991b1b"


def _gamma_fit_spi(historical: np.ndarray, value: float) -> float | None:
    """
    Transform `value` to an SPI score using a gamma distribution
    fitted to `historical`.  Returns None if fitting fails.

    Minimum 2 reference values are accepted; reliability improves
    significantly with ≥ 10 per calendar month (WMO recommends 30 years).
    """
    if len(historical) < 2:
        return None

    p_zero = float(np.mean(historical == 0))
    non_zero = historical[historical > 0]

    if len(non_zero) < 2:
        return None

    try:
        alpha, loc, beta = stats.gamma.fit(non_zero, floc=0)
        if value == 0:
            p = p_zero / 2.0
        else:
            p_gamma = float(stats.gamma.cdf(value, alpha, loc=loc, scale=beta))
            p = p_zero + (1.0 - p_zero) * p_gamma
    except Exception as exc:
        logger.debug("Gamma fit failed: %s", exc)
        return None

    return float(stats.norm.ppf(np.clip(p, 1e-6, 1.0 - 1e-6)))


def compute_spi(
    monthly_data: list[tuple[int, int, float]],
    timescale: int,
) -> list[tuple[int, int, float | None, int]]:
    """
    Compute SPI for a chronologically-sorted time series of monthly totals.

    Args:
        monthly_data: list of (year, month, precip_mm) in ascending date order.
        timescale:    accumulation window in months (1, 3, or 6).

    Returns:
        list of (year, month, spi_value, n_reference).
        spi_value is None when the window is incomplete or the per-calendar-month
        sample has fewer than 2 values.
        n_reference is the number of same-month reference values used.
    """
    n = len(monthly_data)
    if n < timescale:
        return [(y, m, None, 0) for y, m, _ in monthly_data]

    precips = np.array([p for _, _, p in monthly_data], dtype=float)

    # Rolling sum: value at index i is the sum of the timescale months ending at i.
    rolling: list[float | None] = [None] * (timescale - 1)
    for i in range(timescale - 1, n):
        rolling.append(float(np.sum(precips[i - timescale + 1 : i + 1])))

    results: list[tuple[int, int, float | None, int]] = []
    for i, (year, month, _) in enumerate(monthly_data):
        val = rolling[i]
        if val is None:
            results.append((year, month, None, 0))
            continue

        # Historical distribution: same calendar month, up to index i (no look-ahead).
        hist = np.array(
            [
                rolling[j]
                for j in range(timescale - 1, i + 1)
                if monthly_data[j][1] == month and rolling[j] is not None
            ],
            dtype=float,
        )

        spi = _gamma_fit_spi(hist, val)
        results.append((year, month, spi, len(hist)))

    return results


async def recompute_spi(db) -> int:
    """
    Recompute all SPI records in the DB from ObservedRainfall data.
    Deletes existing records for affected sources and inserts fresh ones.
    Returns the total number of month×timescale rows written.
    """
    from sqlalchemy import delete, select

    from app.models.observed_rainfall import ObservedRainfall
    from app.models.spi import SPIRecord

    result = await db.execute(
        select(
            ObservedRainfall.obs_date,
            ObservedRainfall.precip_mean,
            ObservedRainfall.source,
        ).order_by(ObservedRainfall.obs_date)
    )
    rows = result.all()
    if not rows:
        return 0

    # Aggregate daily → monthly per source.
    monthly: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    for obs_date, precip_mean, source in rows:
        monthly[(source, obs_date.year, obs_date.month)].append(precip_mean)

    sources = {k[0] for k in monthly}
    total = 0
    now = datetime.now(timezone.utc)

    for source in sources:
        keys = sorted(
            (k for k in monthly if k[0] == source),
            key=lambda k: (k[1], k[2]),
        )
        monthly_totals = [(y, m, sum(monthly[(source, y, m)])) for _, y, m in keys]
        n_days_map = {(y, m): len(monthly[(source, y, m)]) for _, y, m in keys}

        # spi_by_scale[(ts)][(year, month)] = (spi_value, n_reference)
        spi_by_scale: dict[int, dict[tuple[int, int], tuple[float | None, int]]] = {}
        for ts in TIMESCALES:
            spi_by_scale[ts] = {
                (y, m): (v, n_ref)
                for y, m, v, n_ref in compute_spi(monthly_totals, ts)
            }

        await db.execute(delete(SPIRecord).where(SPIRecord.source == source))

        for y, m, precip_total in monthly_totals:
            for ts in TIMESCALES:
                spi_val, n_ref = spi_by_scale[ts].get((y, m), (None, 0))
                db.add(
                    SPIRecord(
                        source=source,
                        year=y,
                        month=m,
                        timescale=ts,
                        monthly_precip_mm=precip_total,
                        n_days=n_days_map[(y, m)],
                        spi_value=spi_val,
                        n_reference=n_ref,
                        computed_at=now,
                    )
                )
        total += len(monthly_totals) * len(TIMESCALES)

    await db.commit()
    logger.info("SPI recomputed: %d records written across %d source(s)", total, len(sources))
    return total
