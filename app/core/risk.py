"""Composite drought risk score combining SPI, seasonal outlook, and active triggers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.seasonal import SeasonalForecast


def compute_risk_score(
    current_spi: dict,
    latest_seasonal: "SeasonalForecast | None",
    n_active_triggers: int,
) -> dict:
    """Return a 0–100 composite risk score with component breakdown.

    Weights: SPI 40 pts · Seasonal outlook 30 pts · Active triggers 30 pts.
    """

    # ── SPI component (0–40) ─────────────────────────────────────────────────
    worst_spi = None
    for ts in [6, 3, 1]:
        info = current_spi.get(ts)
        if info and info.get("spi") is not None:
            v = info["spi"]
            if worst_spi is None or v < worst_spi:
                worst_spi = v

    if worst_spi is None or worst_spi > -0.5:
        spi_pts = 0
    elif worst_spi > -1.0:
        spi_pts = 10
    elif worst_spi > -1.5:
        spi_pts = 20
    elif worst_spi > -2.0:
        spi_pts = 30
    else:
        spi_pts = 40

    # ── Seasonal component (0–30) ────────────────────────────────────────────
    seasonal_pts = 0
    if latest_seasonal and latest_seasonal.below_normal_pct is not None:
        bn = latest_seasonal.below_normal_pct
        if bn >= 50:
            seasonal_pts = 30
        elif bn >= 40:
            seasonal_pts = 20
        elif bn >= 33:
            seasonal_pts = 10

    # ── Active trigger component (0–30) ─────────────────────────────────────
    if n_active_triggers >= 3:
        trigger_pts = 30
    elif n_active_triggers == 2:
        trigger_pts = 20
    elif n_active_triggers == 1:
        trigger_pts = 10
    else:
        trigger_pts = 0

    total = spi_pts + seasonal_pts + trigger_pts

    if total >= 75:
        level, level_color = "Extreme", "#dc2626"
    elif total >= 50:
        level, level_color = "High", "#f97316"
    elif total >= 25:
        level, level_color = "Moderate", "#f59e0b"
    else:
        level, level_color = "Low", "#22c55e"

    has_data = bool(current_spi) or latest_seasonal is not None or n_active_triggers > 0

    return {
        "total": total,
        "level": level,
        "level_color": level_color,
        "spi_pts": spi_pts,
        "seasonal_pts": seasonal_pts,
        "trigger_pts": trigger_pts,
        "worst_spi": worst_spi,
        "has_data": has_data,
    }
