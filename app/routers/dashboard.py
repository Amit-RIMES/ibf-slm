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

    total_users = await db.scalar(select(func.count()).select_from(User))
    total_forecasts = await db.scalar(select(func.count()).select_from(ForecastUpload))
    total_impacts = await db.scalar(select(func.count()).select_from(ImpactRecord))

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

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "stats": {
                "total_users": total_users,
                "total_forecasts": total_forecasts,
                "total_impacts": total_impacts,
                "member_since": user.created_at.strftime("%B %d, %Y"),
            },
            "recent_forecasts": recent_forecasts,
            "recent_impacts": recent_impacts,
            "active_activations": active_activations,
        },
    )
