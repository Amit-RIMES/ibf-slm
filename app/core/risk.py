"""Composite drought risk score combining SPI, seasonal outlook, and active triggers."""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models.seasonal import SeasonalForecast

_MONTH_ABBR = [calendar.month_abbr[i] for i in range(1, 13)]


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


async def compute_and_record_risk_score(db: "AsyncSession", source: str = "CHIRPS") -> dict:
    """Query current state, compute risk score, persist one record per day, return score dict."""
    from sqlalchemy import func, select
    from app.core.spi import TIMESCALES, spi_category
    from app.models.risk_history import RiskScoreRecord
    from app.models.seasonal import SeasonalForecast
    from app.models.spi import SPIRecord
    from app.models.trigger import TriggerActivation

    spi_r = await db.execute(
        select(SPIRecord)
        .where(SPIRecord.source == source)
        .order_by(SPIRecord.year, SPIRecord.month, SPIRecord.timescale)
    )
    by_scale: dict[int, list] = {ts: [] for ts in TIMESCALES}
    for rec in spi_r.scalars().all():
        if rec.timescale in by_scale:
            by_scale[rec.timescale].append(rec)

    current_spi: dict = {}
    for ts, recs in by_scale.items():
        latest = next((r for r in reversed(recs) if r.spi_value is not None), None)
        if latest:
            label, colour = spi_category(latest.spi_value)
            current_spi[ts] = {"spi": round(latest.spi_value, 2), "label": label, "colour": colour}

    sf_r = await db.execute(
        select(SeasonalForecast).order_by(SeasonalForecast.issue_date.desc()).limit(1)
    )
    latest_seasonal = sf_r.scalar_one_or_none()

    n_active = await db.scalar(
        select(func.count()).select_from(TriggerActivation)
        .where(TriggerActivation.status == "active")
    ) or 0

    risk = compute_risk_score(current_spi, latest_seasonal, n_active)

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    existing = await db.scalar(
        select(RiskScoreRecord).where(
            RiskScoreRecord.source == source,
            RiskScoreRecord.scored_at >= today_start,
            RiskScoreRecord.scored_at < tomorrow_start,
        )
    )
    if existing:
        existing.total = risk["total"]
        existing.level = risk["level"]
        existing.spi_pts = risk["spi_pts"]
        existing.seasonal_pts = risk["seasonal_pts"]
        existing.trigger_pts = risk["trigger_pts"]
        existing.worst_spi = risk.get("worst_spi")
        existing.scored_at = now
    else:
        db.add(RiskScoreRecord(
            scored_at=now,
            source=source,
            total=risk["total"],
            level=risk["level"],
            spi_pts=risk["spi_pts"],
            seasonal_pts=risk["seasonal_pts"],
            trigger_pts=risk["trigger_pts"],
            worst_spi=risk.get("worst_spi"),
        ))
    await db.commit()
    return risk
