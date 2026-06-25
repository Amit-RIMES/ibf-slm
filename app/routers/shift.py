import calendar as _cal
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.gaps import check_data_gaps
from app.core.risk import compute_risk_score
from app.core.spi import TIMESCALES, spi_category
from app.models.bulletin_draft import BulletinDraft
from app.models.forecast import ForecastUpload
from app.models.observed_rainfall import ObservedRainfall
from app.models.seasonal import SeasonalForecast
from app.models.spi import SPIRecord
from app.models.sync import SyncConfig
from app.models.trigger import TriggerActivation
from app.models.user import User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_MONTH_ABBR = [_cal.month_abbr[i] for i in range(1, 13)]

HAZARD_COLORS = {
    "flood": "#2563eb",
    "storm": "#7c3aed",
    "drought": "#d97706",
    "landslide": "#92400e",
    "heatwave": "#dc2626",
    "cyclone": "#0891b2",
    "other": "#6b7280",
}


def _ago(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    mins = int((now - dt).total_seconds() / 60)
    if mins < 2:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    if mins < 1440:
        return f"{mins // 60}h ago"
    return f"{mins // 1440}d ago"


@router.get("/shift", response_class=HTMLResponse)
async def shift_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    now = datetime.now(timezone.utc)

    # Active alerts
    acts_r = await db.execute(
        select(TriggerActivation)
        .where(TriggerActivation.status == "active")
        .order_by(desc(TriggerActivation.triggered_at))
    )
    raw_alerts = acts_r.scalars().all()

    # Enrich with severity and time-ago
    alerts = []
    for act in raw_alerts:
        trig = act.trigger
        excess_pct = None
        if trig and trig.threshold:
            if trig.operator in ("gt", "gte"):
                excess_pct = round((act.value - trig.threshold) / abs(trig.threshold) * 100, 1)
            else:
                excess_pct = round((trig.threshold - act.value) / abs(trig.threshold) * 100, 1)
        alerts.append({
            "activation": act,
            "trigger": trig,
            "excess_pct": excess_pct,
            "ago": _ago(act.triggered_at, now),
            "color": HAZARD_COLORS.get(trig.hazard_type if trig else "other", "#6b7280"),
        })
    alerts.sort(key=lambda x: x["excess_pct"] or 0, reverse=True)

    # Latest forecast
    latest_fc = await db.scalar(
        select(ForecastUpload).order_by(desc(ForecastUpload.uploaded_at))
    )

    # Latest CHIRPS observation
    last_chirps_date = await db.scalar(
        select(ObservedRainfall.obs_date)
        .where(ObservedRainfall.source == "CHIRPS")
        .order_by(desc(ObservedRainfall.obs_date))
    )

    # Sync config
    sync_cfg = await db.scalar(select(SyncConfig).where(SyncConfig.id == 1))
    next_sync = None
    if sync_cfg and sync_cfg.enabled:
        next_sync = f"{sync_cfg.sync_hour:02d}:{sync_cfg.sync_minute:02d} UTC"

    # Pending bulletin drafts
    pending_drafts = await db.scalar(
        select(func.count()).select_from(BulletinDraft)
        .where(BulletinDraft.status == "pending")
    ) or 0

    # Pending user registrations
    pending_users = await db.scalar(
        select(func.count()).select_from(User).where(User.is_active == False)  # noqa: E712
    ) or 0

    # Anomalous forecasts in last 24 h
    anomalies_24h = await db.scalar(
        select(func.count()).select_from(ForecastUpload)
        .where(
            ForecastUpload.is_anomaly == True,  # noqa: E712
            ForecastUpload.uploaded_at >= now - timedelta(hours=24),
        )
    ) or 0

    # Data gaps
    data_gaps = await check_data_gaps(db)

    # Risk score — same logic as dashboard.py
    spi_r = await db.execute(
        select(SPIRecord).order_by(SPIRecord.year, SPIRecord.month, SPIRecord.timescale)
    )
    spi_current: dict[int, dict] = {}
    by_scale: dict[int, list] = {ts: [] for ts in TIMESCALES}
    for rec in spi_r.scalars().all():
        if rec.timescale in by_scale:
            by_scale[rec.timescale].append(rec)
    for ts, recs in by_scale.items():
        latest = next((r for r in reversed(recs) if r.spi_value is not None), None)
        if latest:
            label, colour = spi_category(latest.spi_value)
            spi_current[ts] = {
                "spi": round(latest.spi_value, 2),
                "label": label,
                "colour": colour,
                "month_name": _MONTH_ABBR[latest.month - 1],
                "year": latest.year,
            }

    sf_r = await db.execute(
        select(SeasonalForecast).order_by(SeasonalForecast.issue_date.desc()).limit(1)
    )
    latest_seasonal = sf_r.scalar_one_or_none()
    risk = compute_risk_score(spi_current, latest_seasonal, len(raw_alerts))

    # Total pending actions count
    total_pending = (
        len(raw_alerts)
        + pending_drafts
        + (pending_users if user.role == "admin" else 0)
    )

    fc_ago = _ago(latest_fc.uploaded_at if latest_fc else None, now)

    return templates.TemplateResponse(request, "shift_dashboard.html", {
        "user": user,
        "now": now,
        "alerts": alerts,
        "alert_count": len(raw_alerts),
        "latest_fc": latest_fc,
        "fc_ago": fc_ago,
        "last_chirps_date": last_chirps_date,
        "sync_cfg": sync_cfg,
        "next_sync": next_sync,
        "risk": risk,
        "pending_drafts": pending_drafts,
        "pending_users": pending_users,
        "anomalies_24h": anomalies_24h,
        "data_gaps": data_gaps,
        "total_pending": total_pending,
    })
