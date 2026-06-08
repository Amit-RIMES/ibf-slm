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
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.trigger import Trigger, TriggerActivation
from app.models.user import User
from app.routers.forecasts import COUNTRY_NAMES

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    date_from: str = "",
    date_to: str = "",
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    # Parse optional chart date range
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

    total_users = await db.scalar(select(func.count()).select_from(User).where(User.is_active == True))  # noqa: E712
    pending_count = await db.scalar(select(func.count()).select_from(User).where(User.is_active == False))  # noqa: E712
    admin_count = await db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active == True))  # noqa: E712
    total_forecasts = await db.scalar(select(func.count()).select_from(ForecastUpload))
    total_impacts = await db.scalar(select(func.count()).select_from(ImpactRecord))

    recent_users_result = await db.execute(
        select(User).order_by(desc(User.created_at)).limit(5)
    )
    recent_users = recent_users_result.scalars().all()

    recent_forecasts_result = await db.execute(
        select(ForecastUpload).order_by(desc(ForecastUpload.uploaded_at)).limit(5)
    )
    recent_forecasts = recent_forecasts_result.scalars().all()

    recent_impacts_result = await db.execute(
        select(ImpactRecord).order_by(desc(ImpactRecord.event_date)).limit(5)
    )
    recent_impacts = recent_impacts_result.scalars().all()

    active_activations_result = await db.execute(
        select(TriggerActivation)
        .where(TriggerActivation.status == "active")
        .order_by(desc(TriggerActivation.triggered_at))
    )
    active_activations = active_activations_result.scalars().all()

    # --- Chart data ---

    # 1. Precipitation trend — filtered or last 60 forecasts
    precip_stmt = select(ForecastUpload.uploaded_at, ForecastUpload.precip_mean, ForecastUpload.filename)
    if dt_from:
        precip_stmt = precip_stmt.where(ForecastUpload.uploaded_at >= dt_from)
    if dt_to:
        precip_stmt = precip_stmt.where(ForecastUpload.uploaded_at < dt_to)
    precip_stmt = precip_stmt.order_by(desc(ForecastUpload.uploaded_at)).limit(60)
    precip_result = await db.execute(precip_stmt)
    precip_rows = list(reversed(precip_result.all()))
    precip_chart = {
        "labels": [r.uploaded_at.strftime("%b %d") for r in precip_rows],
        "values": [round(r.precip_mean, 2) for r in precip_rows],
        "filenames": [r.filename for r in precip_rows],
    }

    # 2. Impacts by hazard type — filtered
    from datetime import date as date_type
    hazard_stmt = (
        select(ImpactRecord.hazard_type, func.count().label("cnt"))
        .group_by(ImpactRecord.hazard_type)
    )
    if dt_from:
        hazard_stmt = hazard_stmt.where(ImpactRecord.event_date >= dt_from.date())
    if dt_to:
        hazard_stmt = hazard_stmt.where(ImpactRecord.event_date < (dt_to - timedelta(days=1)).date())
    hazard_stmt = hazard_stmt.order_by(desc("cnt"))
    hazard_result = await db.execute(hazard_stmt)
    hazard_rows = hazard_result.all()
    hazard_chart = {
        "labels": [r.hazard_type.capitalize() for r in hazard_rows],
        "values": [r.cnt for r in hazard_rows],
    }

    # 3. Forecasts ingested per month
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

    # Build month labels spanning the window
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

    # 4. Forecasts by source
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

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "stats": {
                "total_users": total_users,
                "pending_count": pending_count,
                "admin_count": admin_count,
                "user_count": total_users - admin_count,
                "total_forecasts": total_forecasts,
                "total_impacts": total_impacts,
                "member_since": user.created_at.strftime("%B %d, %Y"),
            },
            "recent_users": recent_users,
            "recent_forecasts": recent_forecasts,
            "recent_impacts": recent_impacts,
            "active_activations": active_activations,
            "precip_chart": json.dumps(precip_chart),
            "hazard_chart": json.dumps(hazard_chart),
            "monthly_chart": json.dumps(monthly_chart),
            "source_chart": json.dumps(source_chart),
            "date_from": date_from,
            "date_to": date_to,
        },
    )
