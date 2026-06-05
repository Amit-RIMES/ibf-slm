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
from app.models.trigger import TriggerActivation
from app.models.user import User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

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

    # 1. Precipitation trend: last 30 forecasts, chronological
    precip_result = await db.execute(
        select(ForecastUpload.uploaded_at, ForecastUpload.precip_mean, ForecastUpload.filename)
        .order_by(desc(ForecastUpload.uploaded_at))
        .limit(30)
    )
    precip_rows = list(reversed(precip_result.all()))
    precip_chart = {
        "labels": [r.uploaded_at.strftime("%b %d") for r in precip_rows],
        "values": [round(r.precip_mean, 2) for r in precip_rows],
        "filenames": [r.filename for r in precip_rows],
    }

    # 2. Impacts by hazard type
    hazard_result = await db.execute(
        select(ImpactRecord.hazard_type, func.count().label("cnt"))
        .group_by(ImpactRecord.hazard_type)
        .order_by(desc("cnt"))
    )
    hazard_rows = hazard_result.all()
    hazard_chart = {
        "labels": [r.hazard_type.capitalize() for r in hazard_rows],
        "values": [r.cnt for r in hazard_rows],
    }

    # 3. Forecasts ingested per month (last 6 months)
    six_months_ago = datetime.now(timezone.utc) - timedelta(days=183)
    monthly_result = await db.execute(
        select(ForecastUpload.uploaded_at)
        .where(ForecastUpload.uploaded_at >= six_months_ago)
    )
    monthly_counts: dict[str, int] = defaultdict(int)
    for (uploaded_at,) in monthly_result.all():
        key = uploaded_at.strftime("%b %Y")
        monthly_counts[key] += 1

    # Build ordered list covering the last 6 months even if some are empty
    month_labels = []
    now = datetime.now(timezone.utc)
    for i in range(5, -1, -1):
        dt = now - timedelta(days=30 * i)
        month_labels.append(dt.strftime("%b %Y"))

    monthly_chart = {
        "labels": month_labels,
        "values": [monthly_counts.get(m, 0) for m in month_labels],
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
        },
    )
