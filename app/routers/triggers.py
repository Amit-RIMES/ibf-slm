from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_action
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.email import send_subscriber_alert_emails, send_trigger_activation_email
from app.core.webhook import send_webhook_notifications
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.trigger import (
    OPERATOR_LABELS, OPERATOR_SYMBOLS, OPERATORS, VARIABLES,
    Trigger, TriggerActivation, TriggerSubscription,
)
from app.models.user import User
from app.models.webhook import Webhook

router = APIRouter(prefix="/triggers")
templates = Jinja2Templates(directory="app/templates")

HAZARD_TYPES = ["flood", "storm", "drought", "landslide", "heatwave", "cyclone", "other"]
VARIABLE_LABELS = {
    "precip_mean": "Mean precipitation (mm)",
    "precip_max": "Max precipitation (mm)",
    "precip_min": "Min precipitation (mm)",
}


def _scoped_value(geojson_str: str, variable: str, trigger: "Trigger") -> float:
    """Compute a precip stat restricted to the trigger's bounding box."""
    import json as _json
    fc = _json.loads(geojson_str)
    vals = []
    for feat in fc.get("features", []):
        coords = feat["geometry"]["coordinates"][0]
        clon = (coords[0][0] + coords[2][0]) / 2
        clat = (coords[0][1] + coords[2][1]) / 2
        if (trigger.scope_lat_min <= clat <= trigger.scope_lat_max and
                trigger.scope_lon_min <= clon <= trigger.scope_lon_max):
            vals.append(feat["properties"]["precip"])
    if not vals:
        return 0.0
    if variable == "precip_mean":
        return round(sum(vals) / len(vals), 3)
    if variable == "precip_max":
        return round(max(vals), 3)
    return round(min(vals), 3)  # precip_min


async def evaluate_triggers(forecast: ForecastUpload, db: AsyncSession) -> int:
    """Check all active triggers against a newly ingested forecast. Returns count fired."""
    result = await db.execute(select(Trigger).where(Trigger.is_active == True))  # noqa: E712
    triggers = result.scalars().all()

    global_map = {
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
        if trigger.scope_lat_min is not None:
            value = _scoped_value(forecast.geojson, trigger.variable, trigger)
        else:
            value = global_map.get(trigger.variable, 0.0)
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
        import asyncio
        # Email all admins
        admins_result = await db.execute(
            select(User.email).where(User.role == "admin")
        )
        admin_emails = [row[0] for row in admins_result.all()]
        asyncio.create_task(send_trigger_activation_email(admin_emails, fired_rows))
        # Fire webhooks
        webhooks_result = await db.execute(
            select(Webhook).where(Webhook.is_active == True)  # noqa: E712
        )
        webhooks = webhooks_result.scalars().all()
        asyncio.create_task(send_webhook_notifications(fired_rows, webhooks))

        # Email non-admin subscribers for the triggers they opted into
        fired_trigger_ids = [t.id for t, _, _ in fired_rows]
        subs_result = await db.execute(
            select(User.email, TriggerSubscription.trigger_id)
            .join(TriggerSubscription, TriggerSubscription.user_id == User.id)
            .where(
                TriggerSubscription.trigger_id.in_(fired_trigger_ids),
                User.is_active == True,  # noqa: E712
                User.role != "admin",
            )
        )
        email_to_tids: dict[str, set[int]] = {}
        for email, tid in subs_result.all():
            email_to_tids.setdefault(email, set()).add(tid)
        if email_to_tids:
            asyncio.create_task(send_subscriber_alert_emails(fired_rows, email_to_tids))

    return len(fired_rows)


@router.get("", response_class=HTMLResponse)
async def trigger_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(Trigger).order_by(desc(Trigger.created_at)))
    triggers = result.scalars().all()

    import json as _json
    scoped = [
        {
            "id": t.id,
            "name": t.name,
            "hazard_type": t.hazard_type,
            "is_active": t.is_active,
            "bounds": [[t.scope_lat_min, t.scope_lon_min], [t.scope_lat_max, t.scope_lon_max]],
            "rule": f"{VARIABLE_LABELS[t.variable]} {OPERATOR_SYMBOLS[t.operator]} {t.threshold} mm",
        }
        for t in triggers if t.scope_lat_min is not None
    ]

    return templates.TemplateResponse(
        "trigger_list.html",
        {"request": request, "user": user, "triggers": triggers,
         "OPERATOR_SYMBOLS": OPERATOR_SYMBOLS, "VARIABLE_LABELS": VARIABLE_LABELS,
         "scoped_triggers_json": _json.dumps(scoped),
         "scoped_count": len(scoped)},
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


def _trigger_form_ctx(request, user, trigger_obj, **kwargs):
    return {
        "request": request, "user": user, "trigger": trigger_obj,
        "hazard_types": HAZARD_TYPES, "variables": VARIABLES,
        "operators": OPERATORS, "operator_labels": OPERATOR_LABELS,
        "variable_labels": VARIABLE_LABELS, **kwargs,
    }


def _validate_trigger(name, hazard_type, variable, operator, threshold_str, is_active,
                       scope_enabled=False, scope_lat_min_str="", scope_lat_max_str="",
                       scope_lon_min_str="", scope_lon_max_str=""):
    """Returns (threshold_float, scope_dict_or_None, error_str). error_str is '' on success."""
    if not name.strip():
        return None, None, "Trigger name cannot be empty."
    if hazard_type not in HAZARD_TYPES:
        return None, None, "Please select a valid hazard type."
    if variable not in VARIABLES:
        return None, None, "Please select a valid forecast variable."
    if operator not in OPERATORS:
        return None, None, "Please select a valid operator."
    try:
        threshold = float(threshold_str)
    except (ValueError, TypeError):
        return None, None, "Threshold must be a number (e.g. 25 or 12.5)."
    if threshold < 0:
        return None, None, "Threshold must be zero or greater."

    scope = None
    if scope_enabled:
        try:
            slat_min = float(scope_lat_min_str)
            slat_max = float(scope_lat_max_str)
            slon_min = float(scope_lon_min_str)
            slon_max = float(scope_lon_max_str)
        except (ValueError, TypeError):
            return None, None, "All four bounding box coordinates are required when scope is enabled."
        if not (-90 <= slat_min <= 90 and -90 <= slat_max <= 90):
            return None, None, "Latitude must be between -90 and 90."
        if not (-180 <= slon_min <= 180 and -180 <= slon_max <= 180):
            return None, None, "Longitude must be between -180 and 180."
        if slat_min >= slat_max:
            return None, None, "Min latitude must be less than max latitude."
        if slon_min >= slon_max:
            return None, None, "Min longitude must be less than max longitude."
        scope = {"scope_lat_min": slat_min, "scope_lat_max": slat_max,
                 "scope_lon_min": slon_min, "scope_lon_max": slon_max}

    return threshold, scope, ""


@router.post("/new")
async def trigger_create(
    request: Request,
    name: str = Form(...),
    hazard_type: str = Form(...),
    variable: str = Form(...),
    operator: str = Form(...),
    threshold: str = Form(...),
    is_active: Optional[str] = Form(None),
    scope_enabled: Optional[str] = Form(None),
    scope_lat_min: str = Form(""),
    scope_lat_max: str = Form(""),
    scope_lon_min: str = Form(""),
    scope_lon_max: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    threshold_val, scope, error = _validate_trigger(
        name, hazard_type, variable, operator, threshold, is_active,
        scope_enabled == "on", scope_lat_min, scope_lat_max, scope_lon_min, scope_lon_max,
    )
    if error:
        stub = SimpleNamespace(
            id=None, name=name, hazard_type=hazard_type, variable=variable,
            operator=operator, threshold=threshold, is_active=(is_active == "on"),
            scope_lat_min=scope_lat_min, scope_lat_max=scope_lat_max,
            scope_lon_min=scope_lon_min, scope_lon_max=scope_lon_max,
        )
        return templates.TemplateResponse(
            "trigger_form.html", _trigger_form_ctx(request, user, stub,
                error=error, scope_enabled=(scope_enabled == "on"))
        )

    trigger = Trigger(
        name=name, hazard_type=hazard_type, variable=variable,
        operator=operator, threshold=threshold_val, is_active=is_active == "on",
        **(scope or {}),
    )
    db.add(trigger)
    await db.commit()
    await db.refresh(trigger)
    await log_action(db, user.id, "trigger.create", f"Created trigger '{name}' ({hazard_type})")
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

    # Load impacts linked to any activation of this trigger
    activation_ids = [a.id for a in activations]
    impacts_by_activation: dict[int, list] = {a.id: [] for a in activations}
    if activation_ids:
        impacts_result = await db.execute(
            select(ImpactRecord)
            .where(ImpactRecord.trigger_activation_id.in_(activation_ids))
            .order_by(desc(ImpactRecord.event_date))
        )
        for imp in impacts_result.scalars().all():
            impacts_by_activation[imp.trigger_activation_id].append(imp)

    validated_count = sum(1 for a in activations if impacts_by_activation.get(a.id))

    return templates.TemplateResponse(
        "trigger_detail.html",
        {"request": request, "user": user, "trigger": trigger,
         "activations": activations, "OPERATOR_SYMBOLS": OPERATOR_SYMBOLS,
         "VARIABLE_LABELS": VARIABLE_LABELS,
         "impacts_by_activation": impacts_by_activation,
         "validated_count": validated_count},
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
        "trigger_form.html", _trigger_form_ctx(request, user, trigger)
    )


@router.post("/{trigger_id}/edit")
async def trigger_update(
    trigger_id: int,
    request: Request,
    name: str = Form(...),
    hazard_type: str = Form(...),
    variable: str = Form(...),
    operator: str = Form(...),
    threshold: str = Form(...),
    is_active: Optional[str] = Form(None),
    scope_enabled: Optional[str] = Form(None),
    scope_lat_min: str = Form(""),
    scope_lat_max: str = Form(""),
    scope_lon_min: str = Form(""),
    scope_lon_max: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = result.scalar_one_or_none()
    if not trigger:
        return RedirectResponse("/triggers")

    threshold_val, scope, error = _validate_trigger(
        name, hazard_type, variable, operator, threshold, is_active,
        scope_enabled == "on", scope_lat_min, scope_lat_max, scope_lon_min, scope_lon_max,
    )
    if error:
        stub = SimpleNamespace(
            id=trigger_id, name=name, hazard_type=hazard_type, variable=variable,
            operator=operator, threshold=threshold, is_active=(is_active == "on"),
            scope_lat_min=scope_lat_min, scope_lat_max=scope_lat_max,
            scope_lon_min=scope_lon_min, scope_lon_max=scope_lon_max,
        )
        return templates.TemplateResponse(
            "trigger_form.html", _trigger_form_ctx(request, user, stub,
                error=error, scope_enabled=(scope_enabled == "on"))
        )

    trigger.name = name
    trigger.hazard_type = hazard_type
    trigger.variable = variable
    trigger.operator = operator
    trigger.threshold = threshold_val
    trigger.is_active = is_active == "on"
    if scope:
        trigger.scope_lat_min = scope["scope_lat_min"]
        trigger.scope_lat_max = scope["scope_lat_max"]
        trigger.scope_lon_min = scope["scope_lon_min"]
        trigger.scope_lon_max = scope["scope_lon_max"]
    else:
        trigger.scope_lat_min = None
        trigger.scope_lat_max = None
        trigger.scope_lon_min = None
        trigger.scope_lon_max = None
    await db.commit()
    await log_action(db, user.id, "trigger.edit", f"Edited trigger '{name}'")
    return RedirectResponse(f"/triggers/{trigger_id}", status_code=303)


@router.post("/{trigger_id}/delete")
async def trigger_delete(trigger_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = result.scalar_one_or_none()
    if trigger:
        tname = trigger.name
        await db.delete(trigger)
        await db.commit()
        await log_action(db, user.id, "trigger.delete", f"Deleted trigger '{tname}'")
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
        await log_action(db, user.id, "trigger.acknowledge",
                         f"Acknowledged '{activation.trigger.name}' (value: {activation.value} mm)")
        return RedirectResponse(f"/triggers/{activation.trigger_id}", status_code=303)
    return RedirectResponse("/triggers", status_code=303)
