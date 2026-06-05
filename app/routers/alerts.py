import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.trigger import TriggerActivation

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/alerts", response_class=HTMLResponse)
async def alert_map(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(
        select(TriggerActivation)
        .where(TriggerActivation.status == "active")
        .order_by(TriggerActivation.triggered_at.desc())
    )
    activations = result.scalars().all()

    alerts_json = []
    for a in activations:
        if not a.forecast:
            continue
        fc = a.forecast
        t = a.trigger
        from app.models.trigger import OPERATOR_SYMBOLS, VARIABLE_LABELS
        alerts_json.append({
            "id": a.id,
            "trigger_id": t.id,
            "trigger_name": t.name,
            "hazard_type": t.hazard_type,
            "variable_label": VARIABLE_LABELS.get(t.variable, t.variable),
            "operator_symbol": OPERATOR_SYMBOLS.get(t.operator, t.operator),
            "threshold": t.threshold,
            "value": round(a.value, 3),
            "triggered_at": a.triggered_at.strftime("%Y-%m-%d %H:%M"),
            "forecast_filename": fc.filename,
            "forecast_id": fc.id,
            "lat_min": fc.lat_min,
            "lat_max": fc.lat_max,
            "lon_min": fc.lon_min,
            "lon_max": fc.lon_max,
        })

    return templates.TemplateResponse(
        "alerts.html",
        {
            "request": request,
            "user": user,
            "activations": activations,
            "alerts_json": json.dumps(alerts_json),
        },
    )
