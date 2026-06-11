import json
from collections import defaultdict
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
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.seasonal import SeasonalForecast
from app.models.spi import SPIRecord
from app.models.trigger import Trigger, TriggerActivation
from app.models.user import User
from app.routers.forecasts import COUNTRY_NAMES

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


HAZARD_TYPES = ["flood", "storm", "drought", "landslide", "heatwave", "cyclone", "other"]


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    date_from: str = "",
    date_to: str = "",
    hazard: str = "",
    country: str = "",
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    from sqlalchemy import and_
    from datetime import date as date_type

    # Validate / parse filters
    if hazard and hazard not in HAZARD_TYPES:
        hazard = ""

    dt_from = dt_to = None
    if date_from:
        try:
            dt_from = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
        except ValueError:
            date_from = ""
    if date_to:
        try:
            dt_to = (datetime.fromisoformat(date_to) + timedelta(days=1)).replace(tzinfo=timezone.utc)
        except ValueError:
            date_to = ""

    # Impact filters (hazard + country + date) — used for stat counter, hazard chart, recent table
    impact_filters = []
    if dt_from:
        impact_filters.append(ImpactRecord.event_date >= dt_from.date())
    if dt_to:
        impact_filters.append(ImpactRecord.event_date < (dt_to - timedelta(days=1)).date())
    if hazard:
        impact_filters.append(ImpactRecord.hazard_type == hazard)
    if country:
        impact_filters.append(ImpactRecord.country.ilike(f"%{country}%"))

    total_users = await db.scalar(select(func.count()).select_from(User).where(User.is_active == True))  # noqa: E712
    pending_count = await db.scalar(select(func.count()).select_from(User).where(User.is_active == False))  # noqa: E712
    admin_count = await db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active == True))  # noqa: E712
    total_forecasts = await db.scalar(select(func.count()).select_from(ForecastUpload))

    impacts_count_stmt = select(func.count()).select_from(ImpactRecord)
    if impact_filters:
        impacts_count_stmt = impacts_count_stmt.where(and_(*impact_filters))
    total_impacts = await db.scalar(impacts_count_stmt)
    total_impacts_unfiltered = await db.scalar(select(func.count()).select_from(ImpactRecord))

    recent_users_result = await db.execute(
        select(User).order_by(desc(User.created_at)).limit(5)
    )
    recent_users = recent_users_result.scalars().all()

    recent_forecasts_result = await db.execute(
        select(ForecastUpload).order_by(desc(ForecastUpload.uploaded_at)).limit(5)
    )
    recent_forecasts = recent_forecasts_result.scalars().all()

    recent_impacts_stmt = select(ImpactRecord).order_by(desc(ImpactRecord.event_date)).limit(10)
    if impact_filters:
        recent_impacts_stmt = recent_impacts_stmt.where(and_(*impact_filters))
    recent_impacts = (await db.execute(recent_impacts_stmt)).scalars().all()

    active_activations_result = await db.execute(
        select(TriggerActivation)
        .where(TriggerActivation.status == "active")
        .order_by(desc(TriggerActivation.triggered_at))
    )
    active_activations = active_activations_result.scalars().all()

    # Anomalous forecasts ingested in the last 7 days
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    anomalies_result = await db.execute(
        select(ForecastUpload)
        .where(ForecastUpload.is_anomaly == True)  # noqa: E712
        .where(ForecastUpload.uploaded_at >= week_ago)
        .order_by(desc(ForecastUpload.uploaded_at))
    )
    recent_anomalies = anomalies_result.scalars().all()

    # --- Chart data ---

    # 1. Precipitation trend — date filter only (forecasts don't carry hazard/country)
    precip_stmt = select(ForecastUpload.uploaded_at, ForecastUpload.precip_mean, ForecastUpload.filename)
    if dt_from:
        precip_stmt = precip_stmt.where(ForecastUpload.uploaded_at >= dt_from)
    if dt_to:
        precip_stmt = precip_stmt.where(ForecastUpload.uploaded_at < dt_to)
    precip_stmt = precip_stmt.order_by(desc(ForecastUpload.uploaded_at)).limit(60)
    precip_rows = list(reversed((await db.execute(precip_stmt)).all()))
    precip_chart = {
        "labels": [r.uploaded_at.strftime("%b %d") for r in precip_rows],
        "values": [round(r.precip_mean, 2) for r in precip_rows],
        "filenames": [r.filename for r in precip_rows],
    }

    # 2. Impacts by hazard — country + date filters (not hazard, so the chart always shows the full breakdown)
    hazard_chart_filters = []
    if dt_from:
        hazard_chart_filters.append(ImpactRecord.event_date >= dt_from.date())
    if dt_to:
        hazard_chart_filters.append(ImpactRecord.event_date < (dt_to - timedelta(days=1)).date())
    if country:
        hazard_chart_filters.append(ImpactRecord.country.ilike(f"%{country}%"))
    hazard_stmt = select(ImpactRecord.hazard_type, func.count().label("cnt")).group_by(ImpactRecord.hazard_type)
    if hazard_chart_filters:
        hazard_stmt = hazard_stmt.where(and_(*hazard_chart_filters))
    hazard_stmt = hazard_stmt.order_by(desc("cnt"))
    hazard_rows = (await db.execute(hazard_stmt)).all()
    hazard_chart = {
        "labels": [r.hazard_type.capitalize() for r in hazard_rows],
        "values": [r.cnt for r in hazard_rows],
        "highlight": hazard.capitalize() if hazard else "",
    }

    # 3. Forecasts ingested per month — date filter only
    now = datetime.now(timezone.utc)
    if dt_from or dt_to:
        window_start = dt_from or (now - timedelta(days=365))
        window_end = (dt_to - timedelta(days=1)) if dt_to else now
    else:
        window_start = now - timedelta(days=183)
        window_end = now

    monthly_result = await db.execute(
        select(ForecastUpload.uploaded_at)
        .where(ForecastUpload.uploaded_at >= window_start)
        .where(ForecastUpload.uploaded_at <= window_end)
    )
    monthly_counts: dict[str, int] = defaultdict(int)
    for (uploaded_at,) in monthly_result.all():
        monthly_counts[uploaded_at.strftime("%b %Y")] += 1

    month_labels = []
    cur = window_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_month = window_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur <= end_month:
        month_labels.append(cur.strftime("%b %Y"))
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)

    monthly_chart = {
        "labels": month_labels,
        "values": [monthly_counts.get(m, 0) for m in month_labels],
    }

    # 4. Forecasts by source — date filter only
    source_label_map = {
        "manual": "Manual upload",
        "regional_rimes": "Regional — RIMES",
        "regional_sea": "Regional — SEA",
        **{f"country_{cc}": f"{name} ({cc.upper()})" for cc, name in COUNTRY_NAMES.items()},
    }
    source_stmt = (
        select(ForecastUpload.source, func.count().label("cnt"))
        .group_by(ForecastUpload.source)
        .order_by(desc("cnt"))
    )
    if dt_from:
        source_stmt = source_stmt.where(ForecastUpload.uploaded_at >= dt_from)
    if dt_to:
        source_stmt = source_stmt.where(ForecastUpload.uploaded_at < dt_to)
    source_rows = (await db.execute(source_stmt)).all()
    source_chart = {
        "labels": [source_label_map.get(r.source or "", "Unknown") for r in source_rows],
        "values": [r.cnt for r in source_rows],
    }

    impacts_filtered = bool(impact_filters)
    data_gaps = await check_data_gaps(db)

    # ── Composite risk score ──────────────────────────────────────────────────
    import calendar as _cal
    _MONTH_ABBR = [_cal.month_abbr[i] for i in range(1, 13)]

    spi_r = await db.execute(
        select(SPIRecord).order_by(SPIRecord.year, SPIRecord.month, SPIRecord.timescale)
    )
    dash_spi_current: dict[int, dict] = {}
    by_scale_dash: dict[int, list] = {ts: [] for ts in TIMESCALES}
    for rec in spi_r.scalars().all():
        if rec.timescale in by_scale_dash:
            by_scale_dash[rec.timescale].append(rec)
    for ts, recs in by_scale_dash.items():
        latest = next((r for r in reversed(recs) if r.spi_value is not None), None)
        if latest:
            label, colour = spi_category(latest.spi_value)
            dash_spi_current[ts] = {
                "spi": round(latest.spi_value, 2),
                "label": label,
                "colour": colour,
                "month_name": _MONTH_ABBR[latest.month - 1],
                "year": latest.year,
            }

    sf_dash_r = await db.execute(
        select(SeasonalForecast).order_by(SeasonalForecast.issue_date.desc()).limit(1)
    )
    latest_seasonal_dash = sf_dash_r.scalar_one_or_none()

    risk = compute_risk_score(dash_spi_current, latest_seasonal_dash, len(active_activations))

    return templates.TemplateResponse(
    request,
    "dashboard.html",
    {
            "user": user,
            "data_gaps": data_gaps,
            "stats": {
                "total_users": total_users,
                "pending_count": pending_count,
                "admin_count": admin_count,
                "user_count": total_users - admin_count,
                "total_forecasts": total_forecasts,
                "total_impacts": total_impacts,
                "total_impacts_unfiltered": total_impacts_unfiltered,
                "member_since": user.created_at.strftime("%B %d, %Y"),
            },
            "recent_users": recent_users,
            "recent_forecasts": recent_forecasts,
            "recent_impacts": recent_impacts,
            "active_activations": active_activations,
            "recent_anomalies": recent_anomalies,
            "precip_chart": json.dumps(precip_chart),
            "hazard_chart": json.dumps(hazard_chart),
            "monthly_chart": json.dumps(monthly_chart),
            "source_chart": json.dumps(source_chart),
            "date_from": date_from,
            "date_to": date_to,
            "hazard": hazard,
            "country": country,
            "hazard_types": HAZARD_TYPES,
            "impacts_filtered": impacts_filtered,
            "risk": risk,
        },
)
