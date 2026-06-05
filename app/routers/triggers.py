from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.email import send_trigger_activation_email
from app.models.forecast import ForecastUpload
from app.models.trigger import (
    OPERATOR_LABELS, OPERATOR_SYMBOLS, OPERATORS, VARIABLES,
    Trigger, TriggerActivation,
)
from app.models.user import User

router = APIRouter(prefix="/triggers")
templates = Jinja2Templates(directory="app/templates")

HAZARD_TYPES = ["flood", "storm", "drought", "landslide", "heatwave", "cyclone", "other"]
VARIABLE_LABELS = {
    "precip_mean": "Mean precipitation (mm)",
    "precip_max": "Max precipitation (mm)",
    "precip_min": "Min precipitation (mm)",
}


async def evaluate_triggers(forecast: ForecastUpload, db: AsyncSession) -> int:
    """Check all active triggers against a newly ingested forecast. Returns count fired."""
    result = await db.execute(select(Trigger).where(Trigger.is_active == True))  # noqa: E712
    triggers = result.scalars().all()

    value_map = {
        "precip_mean": forecast.precip_mean,
        "precip_max": forecast.precip_max,
        "precip_min": forecast.precip_min,
    }
    ops = {
        "gt":  lambda v, t: v > t,
        "gte": lambda v, t: v >= t,
        "lt":  lambda v, t: v < t,
        "lte": lambda v, t: v <= t,
    }

    fired_rows: list[tuple[Trigger, TriggerActivation, ForecastUpload]] = []
    for trigger in triggers:
        value = value_map.get(trigger.variable, 0.0)
        if ops[trigger.operator](value, trigger.threshold):
            activation = TriggerActivation(
                trigger_id=trigger.id,
                forecast_id=forecast.id,
                value=value,
                status="active",
            )
            db.add(activation)
            fired_rows.append((trigger, activation, forecast))

    if fired_rows:
        await db.commit()
        # Notify all admins — fire-and-forget, errors are logged not raised
        admins_result = await db.execute(
            select(User.email).where(User.role == "admin")
        )
        admin_emails = [row[0] for row in admins_result.all()]
        import asyncio
        asyncio.create_task(send_trigger_activation_email(admin_emails, fired_rows))

    return len(fired_rows)


@router.get("", response_class=HTMLResponse)
async def trigger_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(Trigger).order_by(desc(Trigger.created_at)))
    triggers = result.scalars().all()

    return templates.TemplateResponse(
        "trigger_list.html",
        {"request": request, "user": user, "triggers": triggers,
         "OPERATOR_SYMBOLS": OPERATOR_SYMBOLS, "VARIABLE_LABELS": VARIABLE_LABELS},
    )


@router.get("/new", response_class=HTMLResponse)
async def trigger_new_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    return templates.TemplateResponse(
        "trigger_form.html",
        {"request": request, "user": user, "trigger": None,
         "hazard_types": HAZARD_TYPES, "variables": VARIABLES,
         "operators": OPERATORS, "operator_labels": OPERATOR_LABELS,
         "variable_labels": VARIABLE_LABELS},
    )


@router.post("/new")
async def trigger_create(
    request: Request,
    name: str = Form(...),
    hazard_type: str = Form(...),
    variable: str = Form(...),
    operator: str = Form(...),
    threshold: float = Form(...),
    is_active: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    trigger = Trigger(
        name=name,
        hazard_type=hazard_type,
        variable=variable,
        operator=operator,
        threshold=threshold,
        is_active=is_active == "on",
    )
    db.add(trigger)
    await db.commit()
    await db.refresh(trigger)
    return RedirectResponse(f"/triggers/{trigger.id}", status_code=303)


@router.get("/{trigger_id}", response_class=HTMLResponse)
async def trigger_detail(trigger_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = result.scalar_one_or_none()
    if not trigger:
        return RedirectResponse("/triggers")

    activations_result = await db.execute(
        select(TriggerActivation)
        .where(TriggerActivation.trigger_id == trigger_id)
        .order_by(desc(TriggerActivation.triggered_at))
    )
    activations = activations_result.scalars().all()

    return templates.TemplateResponse(
        "trigger_detail.html",
        {"request": request, "user": user, "trigger": trigger,
         "activations": activations, "OPERATOR_SYMBOLS": OPERATOR_SYMBOLS,
         "VARIABLE_LABELS": VARIABLE_LABELS},
    )


@router.get("/{trigger_id}/edit", response_class=HTMLResponse)
async def trigger_edit_page(trigger_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = result.scalar_one_or_none()
    if not trigger:
        return RedirectResponse("/triggers")

    return templates.TemplateResponse(
        "trigger_form.html",
        {"request": request, "user": user, "trigger": trigger,
         "hazard_types": HAZARD_TYPES, "variables": VARIABLES,
         "operators": OPERATORS, "operator_labels": OPERATOR_LABELS,
         "variable_labels": VARIABLE_LABELS},
    )


@router.post("/{trigger_id}/edit")
async def trigger_update(
    trigger_id: int,
    request: Request,
    name: str = Form(...),
    hazard_type: str = Form(...),
    variable: str = Form(...),
    operator: str = Form(...),
    threshold: float = Form(...),
    is_active: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = result.scalar_one_or_none()
    if not trigger:
        return RedirectResponse("/triggers")

    trigger.name = name
    trigger.hazard_type = hazard_type
    trigger.variable = variable
    trigger.operator = operator
    trigger.threshold = threshold
    trigger.is_active = is_active == "on"
    await db.commit()
    return RedirectResponse(f"/triggers/{trigger_id}", status_code=303)


@router.post("/{trigger_id}/delete")
async def trigger_delete(trigger_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = result.scalar_one_or_none()
    if trigger:
        await db.delete(trigger)
        await db.commit()
    return RedirectResponse("/triggers", status_code=303)


@router.post("/activations/{activation_id}/acknowledge")
async def acknowledge_activation(
    activation_id: int,
    request: Request,
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(
        select(TriggerActivation).where(TriggerActivation.id == activation_id)
    )
    activation = result.scalar_one_or_none()
    if activation:
        activation.status = "acknowledged"
        activation.notes = notes or None
        activation.acknowledged_at = datetime.now(timezone.utc)
        await db.commit()
        return RedirectResponse(f"/triggers/{activation.trigger_id}", status_code=303)
    return RedirectResponse("/triggers", status_code=303)
