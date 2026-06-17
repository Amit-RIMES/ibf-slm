import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_action
from app.core.background import enqueue
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.ensemble import get_exceedance
from app.models.activation_comment import ActivationComment
from app.core.email import send_acknowledgement_emails, send_subscriber_alert_emails, send_trigger_activation_email
from app.core.sms import send_trigger_activation_sms
from app.core.return_period import return_period_for_value, rp_label, rp_color
from app.models.alert_recipient import AlertRecipient
from app.models.return_level import ReturnLevel
from app.models.sms_config import SMSConfig
from app.core.webhook import send_webhook_notifications
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.trigger import (
    FORECAST_VARIABLES, LOGIC_OPS, OPERATOR_LABELS, OPERATOR_SYMBOLS,
    OPERATORS, SPI_VARIABLES, VARIABLES,
    Trigger, TriggerActivation, TriggerSubscription,
)
from app.models.user import User
from app.models.webhook import Webhook

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/triggers")
templates = Jinja2Templates(directory="app/templates")

HAZARD_TYPES = ["flood", "storm", "drought", "landslide", "heatwave", "cyclone", "other"]
VARIABLE_LABELS = {
    "precip_mean": "Mean precipitation (mm)",
    "precip_max": "Max precipitation (mm)",
    "precip_min": "Min precipitation (mm)",
    "spi_1": "SPI-1 (1-month drought index)",
    "spi_3": "SPI-3 (3-month drought index)",
    "spi_6": "SPI-6 (6-month drought index)",
}


def _point_in_polygon(lat: float, lon: float, ring: list) -> bool:
    """Ray-casting point-in-polygon for a GeoJSON coordinate ring [[lon,lat],...]."""
    n, inside = len(ring), False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]   # lon, lat
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            if lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def _scoped_value(geojson_str: str, variable: str, trigger: "Trigger") -> float:
    """Compute a precip stat restricted to the trigger's scope (polygon or bounding box)."""
    import json as _json
    fc = _json.loads(geojson_str)
    ring = _json.loads(trigger.scope_polygon) if trigger.scope_polygon else None
    vals = []
    for feat in fc.get("features", []):
        coords = feat["geometry"]["coordinates"][0]
        clon = (coords[0][0] + coords[2][0]) / 2
        clat = (coords[0][1] + coords[2][1]) / 2
        if ring:
            if not _point_in_polygon(clat, clon, ring):
                continue
        else:
            if not (trigger.scope_lat_min <= clat <= trigger.scope_lat_max and
                    trigger.scope_lon_min <= clon <= trigger.scope_lon_max):
                continue
        vals.append(feat["properties"]["precip"])
    if not vals:
        return 0.0
    if variable == "precip_mean":
        return round(sum(vals) / len(vals), 3)
    if variable == "precip_max":
        return round(max(vals), 3)
    return round(min(vals), 3)  # precip_min


async def evaluate_triggers(forecast: ForecastUpload, db: AsyncSession) -> int:
    """Check all active forecast-variable triggers against a newly ingested forecast. Returns count fired."""
    result = await db.execute(
        select(Trigger).where(
            Trigger.is_active == True,  # noqa: E712
            Trigger.variable.in_(FORECAST_VARIABLES),
        )
    )
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
        is_scoped = trigger.scope_lat_min is not None or trigger.scope_polygon is not None
        if is_scoped:
            value = _scoped_value(forecast.geojson, trigger.variable, trigger)
        else:
            value = global_map.get(trigger.variable, 0.0)

        # ── Probabilistic evaluation ─────────────────────────────────────────
        activation_probability: float | None = None
        if trigger.probability_threshold is not None and forecast.ensemble_size:
            prob = get_exceedance(forecast.exceedance_json, trigger.threshold)
            if prob is None:
                # Ensemble data present but no pre-computed exceedance for this threshold;
                # fall back to deterministic using ensemble mean.
                fires = ops[trigger.operator](value, trigger.threshold)
            else:
                fires = prob >= trigger.probability_threshold
                activation_probability = prob
        else:
            # ── Deterministic evaluation ─────────────────────────────────────
            fires1 = ops[trigger.operator](value, trigger.threshold)

            if trigger.condition_2_variable and trigger.condition_2_operator and trigger.condition_2_threshold is not None:
                if is_scoped:
                    value2 = _scoped_value(forecast.geojson, trigger.condition_2_variable, trigger)
                else:
                    value2 = global_map.get(trigger.condition_2_variable, 0.0)
                fires2 = ops[trigger.condition_2_operator](value2, trigger.condition_2_threshold)
                fires = (fires1 and fires2) if trigger.logic_op == "and" else (fires1 or fires2)
            else:
                fires = fires1

        if fires:
            # Check cooldown BEFORE db.add to avoid autoflush finding the new activation
            cooldown_cutoff = datetime.now(timezone.utc) - timedelta(
                hours=settings.TRIGGER_COOLDOWN_HOURS
            )
            recent = await db.execute(
                select(TriggerActivation.id)
                .where(
                    TriggerActivation.trigger_id == trigger.id,
                    TriggerActivation.triggered_at >= cooldown_cutoff,
                )
                .limit(1)
            )
            in_cooldown = recent.scalar_one_or_none() is not None

            activation = TriggerActivation(
                trigger_id=trigger.id,
                forecast_id=forecast.id,
                value=value,
                probability=activation_probability,
                status="active",
            )
            db.add(activation)

            if in_cooldown:
                logger.debug(
                    "Trigger %d within cooldown (%dh) — activation recorded, notification suppressed",
                    trigger.id, settings.TRIGGER_COOLDOWN_HOURS,
                )
            else:
                fired_rows.append((trigger, activation, forecast))

    if fired_rows:
        await db.commit()
        # Push SSE events
        from app.routers.api import broadcast_activation
        for t, act, _ in fired_rows:
            broadcast_activation(act.id, t.name, t.hazard_type)
        # Email all admins + external alert recipients
        admins_result = await db.execute(
            select(User.email).where(User.role == "admin")
        )
        admin_emails = [row[0] for row in admins_result.all()]
        ext_result = await db.execute(
            select(AlertRecipient).where(AlertRecipient.is_active == True)  # noqa: E712
        )
        ext_recipients = ext_result.scalars().all()
        ext_emails = [r.email for r in ext_recipients]
        enqueue(send_trigger_activation_email(admin_emails + ext_emails, fired_rows))
        # SMS / WhatsApp
        sms_cfg_row = await db.scalar(select(SMSConfig).where(SMSConfig.id == 1))
        if sms_cfg_row and sms_cfg_row.enabled:
            sms_phones = [r.phone for r in ext_recipients if r.phone]
            wa_phones = [
                r.phone for r in ext_recipients
                if r.phone and r.whatsapp_enabled and sms_cfg_row.whatsapp_enabled
            ]
            if sms_phones or wa_phones:
                cfg_dict = {
                    "provider": sms_cfg_row.provider,
                    "enabled": sms_cfg_row.enabled,
                    "account_sid": sms_cfg_row.account_sid,
                    "auth_token": sms_cfg_row.auth_token,
                    "from_number": sms_cfg_row.from_number,
                    "whatsapp_enabled": sms_cfg_row.whatsapp_enabled,
                    "whatsapp_from": sms_cfg_row.whatsapp_from,
                    "webhook_url": sms_cfg_row.webhook_url,
                }
                enqueue(send_trigger_activation_sms(sms_phones, wa_phones, fired_rows, cfg_dict))
        # Fire webhooks
        webhooks_result = await db.execute(
            select(Webhook).where(Webhook.is_active == True)  # noqa: E712
        )
        webhooks = webhooks_result.scalars().all()
        enqueue(send_webhook_notifications(fired_rows, webhooks))

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
            enqueue(send_subscriber_alert_emails(fired_rows, email_to_tids))

    return len(fired_rows)


@router.get("/activations/export.csv")
async def activations_export(
    request: Request,
    db: AsyncSession = Depends(get_db),
    trigger_id: Optional[int] = None,
    hazard: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    from sqlalchemy import and_
    from datetime import datetime as dt

    filters = []
    if trigger_id:
        filters.append(TriggerActivation.trigger_id == trigger_id)
    if status in ("active", "acknowledged"):
        filters.append(TriggerActivation.status == status)
    if date_from:
        try:
            filters.append(TriggerActivation.triggered_at >= dt.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            filters.append(TriggerActivation.triggered_at <= dt.fromisoformat(date_to + "T23:59:59"))
        except ValueError:
            pass

    stmt = (
        select(TriggerActivation)
        .order_by(desc(TriggerActivation.triggered_at))
    )
    if filters:
        stmt = stmt.where(and_(*filters))

    result = await db.execute(stmt)
    activations = result.scalars().all()

    # Optionally filter by hazard (requires checking the trigger)
    if hazard:
        activations = [a for a in activations if a.trigger and a.trigger.hazard_type == hazard]

    # Map activation_id → linked impact IDs
    if activations:
        act_ids = [a.id for a in activations]
        impacts_result = await db.execute(
            select(ImpactRecord.trigger_activation_id, ImpactRecord.id)
            .where(ImpactRecord.trigger_activation_id.in_(act_ids))
        )
        from collections import defaultdict
        impact_map: dict[int, list[int]] = defaultdict(list)
        for aid, iid in impacts_result.all():
            impact_map[aid].append(iid)
    else:
        impact_map = {}

    op_sym = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    var_label = {"precip_mean": "precip_mean", "precip_max": "precip_max", "precip_min": "precip_min"}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "activation_id", "trigger_id", "trigger_name", "hazard_type",
        "rule", "observed_value_mm", "threshold_mm",
        "activation_status", "forecast_filename", "forecast_source",
        "triggered_at", "acknowledged_at", "notes",
        "validated", "linked_impact_ids",
    ])
    for act in activations:
        t = act.trigger
        fc = act.forecast
        rule = f"{t.variable} {op_sym.get(t.operator, t.operator)} {t.threshold}" if t else ""
        imp_ids = impact_map.get(act.id, [])
        writer.writerow([
            act.id, act.trigger_id, t.name if t else "", t.hazard_type if t else "",
            rule, act.value, t.threshold if t else "",
            act.status,
            fc.filename if fc else "", fc.source or "" if fc else "",
            act.triggered_at.strftime("%Y-%m-%d %H:%M:%S"),
            act.acknowledged_at.strftime("%Y-%m-%d %H:%M:%S") if act.acknowledged_at else "",
            act.notes or "",
            "yes" if imp_ids else "no",
            ";".join(str(i) for i in imp_ids),
        ])

    buf.seek(0)
    filename = f"activations_trigger{trigger_id}.csv" if trigger_id else "activations.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/report", response_class=HTMLResponse)
async def trigger_report(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    triggers_result = await db.execute(select(Trigger).order_by(desc(Trigger.created_at)))
    triggers = triggers_result.scalars().all()

    activations_result = await db.execute(select(TriggerActivation))
    all_activations = activations_result.scalars().all()

    # Activation IDs that have at least one linked impact record
    validated_result = await db.execute(
        select(ImpactRecord.trigger_activation_id)
        .where(ImpactRecord.trigger_activation_id.isnot(None))
        .distinct()
    )
    validated_ids = {row[0] for row in validated_result.all()}

    from collections import defaultdict
    acts_by_trigger: dict[int, list] = defaultdict(list)
    for act in all_activations:
        acts_by_trigger[act.trigger_id].append(act)

    def _assess(n: int, hit_rate) -> str:
        if n == 0:
            return "never_fired"
        if n < 3:
            return "insufficient"
        if hit_rate >= 0.67:
            return "reliable"
        if hit_rate >= 0.33:
            return "mixed"
        return "high_false_alarm"

    rows = []
    for t in triggers:
        acts = acts_by_trigger[t.id]
        n = len(acts)
        validated = sum(1 for a in acts if a.id in validated_ids)
        hit_rate = validated / n if n > 0 else None
        avg_val = round(sum(a.value for a in acts) / n, 2) if n else None
        max_val = round(max(a.value for a in acts), 2) if n else None
        last_act = max((a.triggered_at for a in acts), default=None)
        rows.append({
            "trigger": t,
            "activation_count": n,
            "validated_count": validated,
            "false_alarm_count": n - validated,
            "hit_rate": hit_rate,
            "avg_value": avg_val,
            "max_value": max_val,
            "last_activated": last_act,
            "assessment": _assess(n, hit_rate),
        })

    # Sort: most activations first; ties broken by hit rate desc; never-fired last
    rows.sort(key=lambda r: (r["activation_count"] == 0, -(r["activation_count"]), -(r["hit_rate"] or 0)))

    total_activations = len(all_activations)
    total_validated = len(validated_ids & {a.id for a in all_activations})
    overall_hit_rate = total_validated / total_activations if total_activations else None
    never_fired = sum(1 for r in rows if r["activation_count"] == 0)

    return templates.TemplateResponse(
    request,
    "trigger_report.html",
    {
            "user": user, "rows": rows,
            "total_activations": total_activations,
            "total_validated": total_validated,
            "overall_hit_rate": overall_hit_rate,
            "never_fired": never_fired,
            "trigger_count": len(triggers),
            "OPERATOR_SYMBOLS": OPERATOR_SYMBOLS,
            "VARIABLE_LABELS": VARIABLE_LABELS,
        },
)


@router.get("", response_class=HTMLResponse)
async def trigger_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(Trigger).order_by(desc(Trigger.created_at)))
    triggers = result.scalars().all()

    from app.core.performance import compute_trigger_quality
    quality = await compute_trigger_quality(db, list(triggers))

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
    request,
    "trigger_list.html",
    {"user": user, "triggers": triggers,
         "OPERATOR_SYMBOLS": OPERATOR_SYMBOLS, "VARIABLE_LABELS": VARIABLE_LABELS,
         "scoped_triggers_json": _json.dumps(scoped),
         "scoped_count": len(scoped),
         "quality": quality},
)


@router.get("/new", response_class=HTMLResponse)
async def trigger_new_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    return templates.TemplateResponse(
        request, "trigger_form.html",
        {"user": user, "trigger": None,
         "hazard_types": HAZARD_TYPES, "variables": VARIABLES,
         "forecast_variables": FORECAST_VARIABLES, "spi_variables": SPI_VARIABLES,
         "operators": OPERATORS, "operator_labels": OPERATOR_LABELS,
         "variable_labels": VARIABLE_LABELS, "logic_ops": LOGIC_OPS},
    )


def _trigger_form_ctx(request, user, trigger_obj, **kwargs):
    return {
        "request": request, "user": user, "trigger": trigger_obj,
        "hazard_types": HAZARD_TYPES, "variables": VARIABLES,
        "forecast_variables": FORECAST_VARIABLES, "spi_variables": SPI_VARIABLES,
        "operators": OPERATORS, "operator_labels": OPERATOR_LABELS,
        "variable_labels": VARIABLE_LABELS, "logic_ops": LOGIC_OPS,
        **kwargs,
    }


def _validate_trigger(
    name, hazard_type, variable, operator, threshold_str, is_active,
    scope_enabled=False, scope_lat_min_str="", scope_lat_max_str="",
    scope_lon_min_str="", scope_lon_max_str="",
    cond2_enabled=False, cond2_variable="", cond2_operator="", cond2_threshold_str="",
    logic_op="and", scope_polygon_str="",
    prob_enabled=False, probability_threshold_str="",
):
    """Returns (threshold_float, extras_dict, error_str). error_str is '' on success."""
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
        return None, None, "Threshold must be a number (e.g. 25 or -1.5)."
    if variable in SPI_VARIABLES:
        if not (-5.0 <= threshold <= 5.0):
            return None, None, "SPI threshold must be between -5 and 5 (typical range: -2 to 2)."
    elif threshold < 0:
        return None, None, "Threshold must be zero or greater."

    extras: dict = {}

    # Probability threshold (probabilistic mode)
    if prob_enabled and probability_threshold_str:
        try:
            pt = float(probability_threshold_str)
        except (ValueError, TypeError):
            return None, None, "Probability threshold must be a number between 0 and 1 (e.g. 0.7)."
        if not (0.0 < pt <= 1.0):
            return None, None, "Probability threshold must be between 0 (exclusive) and 1."
        extras["probability_threshold"] = pt
    else:
        extras["probability_threshold"] = None

    # Second condition
    if cond2_enabled and cond2_variable and cond2_operator:
        if cond2_variable not in VARIABLES:
            return None, None, "Please select a valid variable for the second condition."
        if cond2_operator not in OPERATORS:
            return None, None, "Please select a valid operator for the second condition."
        try:
            c2t = float(cond2_threshold_str)
        except (ValueError, TypeError):
            return None, None, "Second condition threshold must be a number."
        extras["condition_2_variable"] = cond2_variable
        extras["condition_2_operator"] = cond2_operator
        extras["condition_2_threshold"] = c2t
        extras["logic_op"] = logic_op if logic_op in LOGIC_OPS else "and"
    else:
        extras["condition_2_variable"] = None
        extras["condition_2_operator"] = None
        extras["condition_2_threshold"] = None
        extras["logic_op"] = "and"

    # Polygon scope (takes precedence over bbox)
    polygon_json = scope_polygon_str.strip() if scope_polygon_str else ""
    if polygon_json:
        try:
            ring = json.loads(polygon_json)
            if not isinstance(ring, list) or len(ring) < 3:
                return None, None, "Polygon must be a JSON array of at least 3 [lon, lat] pairs."
            extras["scope_polygon"] = polygon_json
            extras["scope_lat_min"] = None
            extras["scope_lat_max"] = None
            extras["scope_lon_min"] = None
            extras["scope_lon_max"] = None
        except (ValueError, TypeError):
            return None, None, "Polygon must be valid JSON (array of [lon, lat] pairs)."
    elif scope_enabled:
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
        extras["scope_lat_min"] = slat_min
        extras["scope_lat_max"] = slat_max
        extras["scope_lon_min"] = slon_min
        extras["scope_lon_max"] = slon_max
        extras["scope_polygon"] = None
    else:
        extras["scope_lat_min"] = None
        extras["scope_lat_max"] = None
        extras["scope_lon_min"] = None
        extras["scope_lon_max"] = None
        extras["scope_polygon"] = None

    return threshold, extras, ""


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
    cond2_enabled: Optional[str] = Form(None),
    cond2_variable: str = Form(""),
    cond2_operator: str = Form(""),
    cond2_threshold: str = Form(""),
    logic_op: str = Form("and"),
    scope_polygon: str = Form(""),
    prob_enabled: Optional[str] = Form(None),
    probability_threshold: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    threshold_val, extras, error = _validate_trigger(
        name, hazard_type, variable, operator, threshold, is_active,
        scope_enabled == "on", scope_lat_min, scope_lat_max, scope_lon_min, scope_lon_max,
        cond2_enabled == "on", cond2_variable, cond2_operator, cond2_threshold, logic_op, scope_polygon,
        prob_enabled == "on", probability_threshold,
    )
    if error:
        stub = SimpleNamespace(
            id=None, name=name, hazard_type=hazard_type, variable=variable,
            operator=operator, threshold=threshold, is_active=(is_active == "on"),
            scope_lat_min=scope_lat_min, scope_lat_max=scope_lat_max,
            scope_lon_min=scope_lon_min, scope_lon_max=scope_lon_max,
            condition_2_variable=cond2_variable, condition_2_operator=cond2_operator,
            condition_2_threshold=cond2_threshold, logic_op=logic_op, scope_polygon=scope_polygon,
            probability_threshold=probability_threshold,
        )
        return templates.TemplateResponse(
            request, "trigger_form.html", _trigger_form_ctx(request, user, stub,
                error=error, scope_enabled=(scope_enabled == "on"),
                cond2_enabled=(cond2_enabled == "on"),
                prob_enabled=(prob_enabled == "on"))
        )

    trigger = Trigger(
        name=name, hazard_type=hazard_type, variable=variable,
        operator=operator, threshold=threshold_val, is_active=is_active == "on",
        **(extras or {}),
    )
    db.add(trigger)
    await db.commit()
    await db.refresh(trigger)
    await log_action(db, user.id, "trigger.create", f"Created trigger '{name}' ({hazard_type})")
    return RedirectResponse(f"/triggers/{trigger.id}", status_code=303)


@router.get("/{trigger_id}/backtest", response_class=HTMLResponse)
async def trigger_backtest(
    trigger_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    match_window: int = 30,
    date_from: str = "",
    date_to: str = "",
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    from sqlalchemy import and_

    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = result.scalar_one_or_none()
    if not trigger:
        return RedirectResponse("/triggers")

    match_window = max(1, min(match_window, 180))

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

    # All forecasts in the date range
    fc_stmt = select(ForecastUpload).order_by(ForecastUpload.uploaded_at)
    fc_filters = []
    if dt_from:
        fc_filters.append(ForecastUpload.uploaded_at >= dt_from)
    if dt_to:
        fc_filters.append(ForecastUpload.uploaded_at < dt_to)
    if fc_filters:
        fc_stmt = fc_stmt.where(and_(*fc_filters))
    all_forecasts = (await db.execute(fc_stmt)).scalars().all()

    # Impact records matching this trigger's hazard type
    imp_stmt = select(ImpactRecord)
    if trigger.hazard_type:
        imp_stmt = imp_stmt.where(ImpactRecord.hazard_type == trigger.hazard_type)
    all_impacts = (await db.execute(imp_stmt)).scalars().all()

    # Operator evaluation
    _ops = {
        "gt":  lambda v, t: v > t,
        "gte": lambda v, t: v >= t,
        "lt":  lambda v, t: v < t,
        "lte": lambda v, t: v <= t,
    }
    _op = _ops.get(trigger.operator, lambda v, t: False)

    def fires_at(value, threshold):
        return value is not None and _op(value, threshold)

    # Precompute per-forecast: value, date, and whether any impact falls in match window
    fc_data = []
    for fc in all_forecasts:
        v = getattr(fc, trigger.variable, None)
        if v is None:
            continue
        fc_date = fc.uploaded_at.date()
        ws = fc_date - timedelta(days=match_window)
        we = fc_date + timedelta(days=match_window)
        has_impact = any(ws <= imp.event_date <= we for imp in all_impacts)
        fc_data.append({"fc": fc, "value": v, "date": fc_date, "has_impact": has_impact})

    # For each impact: set of fc_data indices that fall in its match window
    imp_windows = []
    for imp in all_impacts:
        ws = imp.event_date - timedelta(days=match_window)
        we = imp.event_date + timedelta(days=match_window)
        covering = frozenset(i for i, fd in enumerate(fc_data) if ws <= fd["date"] <= we)
        imp_windows.append(covering)

    # Probabilistic sweep (only for triggers with probability_threshold configured)
    has_ensemble = trigger.probability_threshold is not None
    roc_json = "null"
    brier_score = None
    reliability_json = "null"

    if has_ensemble and fc_data:
        for fd in fc_data:
            fd["prob"] = get_exceedance(fd["fc"].exceedance_json, trigger.threshold)

        ens_indices = [i for i, fd in enumerate(fc_data) if fd["prob"] is not None]

        if ens_indices:
            bs_total = sum(
                (fc_data[i]["prob"] - (1.0 if fc_data[i]["has_impact"] else 0.0)) ** 2
                for i in ens_indices
            )
            brier_score = round(bs_total / len(ens_indices), 4)

            prob_steps = [round(k * 0.05, 2) for k in range(1, 20)]  # 0.05 … 0.95
            if all(abs(pt - trigger.probability_threshold) > 0.02 for pt in prob_steps):
                prob_steps.append(round(trigger.probability_threshold, 4))
                prob_steps.sort()

            roc_points = []
            for pt in reversed(prob_steps):  # high→low so ROC goes (0,0)→(1,1)
                fire_set = frozenset(
                    i for i, fd in enumerate(fc_data)
                    if fd.get("prob") is not None and fd["prob"] >= pt
                )
                n_fires = len(fire_set)
                n_hits = sum(1 for i in fire_set if fc_data[i]["has_impact"])
                n_fa = n_fires - n_hits
                n_missed = sum(1 for iw in imp_windows if not (iw & fire_set))
                pod = round(n_hits / (n_hits + n_missed), 4) if (n_hits + n_missed) > 0 else None
                far = round(n_fa / n_fires, 4) if n_fires > 0 else None
                roc_points.append({
                    "pt": pt, "pod": pod, "far": far,
                    "fires": n_fires, "hits": n_hits, "fa": n_fa, "missed": n_missed,
                    "is_current": abs(pt - trigger.probability_threshold) < 1e-6,
                })
            roc_json = json.dumps(roc_points)

            bins: list[list[int]] = [[] for _ in range(10)]
            for i in ens_indices:
                bin_idx = min(int(fc_data[i]["prob"] * 10), 9)
                bins[bin_idx].append(1 if fc_data[i]["has_impact"] else 0)
            reliability = [
                {"bin": round((j + 0.5) / 10, 2), "obs_freq": round(sum(b) / len(b), 4), "n": len(b)}
                for j, b in enumerate(bins) if b
            ]
            reliability_json = json.dumps(reliability)

    def compute_stats(threshold):
        fire_set = {i for i, fd in enumerate(fc_data) if fires_at(fd["value"], threshold)}
        n_fires = len(fire_set)
        n_hits = sum(1 for i in fire_set if fc_data[i]["has_impact"])
        n_fa = n_fires - n_hits
        n_missed = sum(1 for iw in imp_windows if not (iw & fire_set))
        pod = round(n_hits / (n_hits + n_missed) * 100, 1) if (n_hits + n_missed) > 0 else None
        far = round(n_fa / n_fires * 100, 1) if n_fires > 0 else None
        csi = round(n_hits / (n_hits + n_missed + n_fa) * 100, 1) if (n_hits + n_missed + n_fa) > 0 else None
        return {"fires": n_fires, "hits": n_hits, "false_alarms": n_fa,
                "missed": n_missed, "pod": pod, "far": far, "csi": csi}

    # Threshold sweep — 30 evenly-spaced points spanning the observed range + current threshold
    values = [fd["value"] for fd in fc_data]
    sweep_json = "[]"
    if values:
        v_min, v_max = min(values), max(values)
        span = v_max - v_min if v_max != v_min else max(v_max, 1.0)
        sweep_min = max(0.0, v_min - span * 0.15)
        sweep_max = v_max + span * 0.15
        N = 30
        step = (sweep_max - sweep_min) / (N - 1)
        raw_thresholds = [round(sweep_min + i * step, 2) for i in range(N)]
        # Insert actual threshold if not already close to a sweep point
        if all(abs(t - trigger.threshold) > step * 0.4 for t in raw_thresholds):
            raw_thresholds.append(round(trigger.threshold, 2))
        thresholds = sorted(set(raw_thresholds))

        sweep = []
        for t in thresholds:
            s = compute_stats(t)
            sweep.append({
                "threshold": round(t, 2),
                "fires": s["fires"],
                "pod": s["pod"],
                "far": s["far"],
                "csi": s["csi"],
                "is_current": abs(t - trigger.threshold) < 1e-6,
            })
        sweep_json = json.dumps(sweep)

    # Threshold recommendation — pick the sweep point with highest CSI (if better than current)
    recommended_threshold = None
    recommended_csi = None
    if sweep_json != "[]":
        import json as _json
        _sweep = _json.loads(sweep_json)
        _current_csi = next((s["csi"] for s in _sweep if s["is_current"]), None) or 0
        _candidates = [s for s in _sweep if not s["is_current"] and s["csi"] is not None]
        if _candidates:
            _best = max(_candidates, key=lambda s: s["csi"])
            if _best["csi"] > _current_csi:
                recommended_threshold = _best["threshold"]
                recommended_csi = _best["csi"]

    # Current threshold detailed stats
    current = compute_stats(trigger.threshold)

    # Fires timeline at current threshold (most recent first)
    fire_indices = {i for i, fd in enumerate(fc_data) if fires_at(fd["value"], trigger.threshold)}
    timeline = sorted(
        [{"fc": fc_data[i]["fc"], "value": fc_data[i]["value"],
          "is_hit": fc_data[i]["has_impact"], "date": fc_data[i]["date"]}
         for i in fire_indices],
        key=lambda x: x["date"], reverse=True,
    )

    return templates.TemplateResponse(
    request,
    "trigger_backtest.html",
    {
            "user": user,
            "trigger": trigger,
            "total_forecasts": len(fc_data),
            "total_impacts": len(all_impacts),
            "match_window": match_window,
            "date_from": date_from,
            "date_to": date_to,
            "current": current,
            "sweep_json": sweep_json,
            "current_threshold": trigger.threshold,
            "timeline": timeline,
            "OPERATOR_SYMBOLS": OPERATOR_SYMBOLS,
            "VARIABLE_LABELS": VARIABLE_LABELS,
            "has_ensemble": has_ensemble,
            "roc_json": roc_json,
            "brier_score": brier_score,
            "reliability_json": reliability_json,
            "recommended_threshold": recommended_threshold,
            "recommended_csi": recommended_csi,
        },
)


@router.post("/{trigger_id}/apply-threshold", response_class=HTMLResponse)
async def trigger_apply_threshold(
    trigger_id: int,
    request: Request,
    threshold: float = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/login", status_code=303)

    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = result.scalar_one_or_none()
    if trigger:
        old = trigger.threshold
        trigger.threshold = threshold
        await db.commit()
        await log_action(db, user.id, "trigger.update",
                         f"Applied recommended threshold {threshold} (was {old}) for trigger '{trigger.name}'")
    return RedirectResponse(f"/triggers/{trigger_id}/backtest", status_code=303)


@router.get("/{trigger_id}/export.geojson")
async def trigger_export_geojson(
    trigger_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import JSONResponse
    import json as _json

    user = await get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = result.scalar_one_or_none()
    if not trigger:
        return JSONResponse({"error": "not found"}, status_code=404)

    if trigger.scope_polygon:
        try:
            ring = _json.loads(trigger.scope_polygon)
            geometry = {"type": "Polygon", "coordinates": [ring]}
        except Exception:
            geometry = None
    elif trigger.scope_lat_min is not None and trigger.scope_lon_min is not None:
        lon1, lat1 = trigger.scope_lon_min, trigger.scope_lat_min
        lon2, lat2 = trigger.scope_lon_max, trigger.scope_lat_max
        geometry = {
            "type": "Polygon",
            "coordinates": [[
                [lon1, lat1], [lon2, lat1], [lon2, lat2], [lon1, lat2], [lon1, lat1],
            ]],
        }
    else:
        geometry = None

    feature = {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "id": trigger.id,
            "name": trigger.name,
            "hazard_type": trigger.hazard_type,
            "variable": trigger.variable,
            "operator": trigger.operator,
            "threshold": trigger.threshold,
            "is_active": trigger.is_active,
        },
    }
    return JSONResponse(
        feature,
        headers={"Content-Disposition": f'attachment; filename="trigger-{trigger_id}.geojson"'},
    )


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

    # Load comments for all activations
    comments_by_activation: dict[int, list] = {a.id: [] for a in activations}
    if activation_ids:
        comments_result = await db.execute(
            select(ActivationComment)
            .where(ActivationComment.activation_id.in_(activation_ids))
            .order_by(ActivationComment.created_at)
        )
        for c in comments_result.scalars().all():
            comments_by_activation[c.activation_id].append(c)

    # Lead-time evolution: for all activations, get forecast uploaded_at and time_start to compute lead time
    act_with_fc = await db.execute(
        select(TriggerActivation, ForecastUpload)
        .join(ForecastUpload, TriggerActivation.forecast_id == ForecastUpload.id)
        .where(TriggerActivation.trigger_id == trigger.id)
        .order_by(ForecastUpload.uploaded_at)
    )
    lead_time_data = []
    for act, fc in act_with_fc.all():
        uploaded = fc.uploaded_at
        if uploaded.tzinfo is None:
            uploaded = uploaded.replace(tzinfo=timezone.utc)
        # Try to compute absolute lead days; fall back to None when time_start is
        # a relative offset string like "T+024h" rather than an ISO date.
        lead_days: int | None = None
        try:
            ts = fc.time_start
            event_start = ts if hasattr(ts, "date") else datetime.fromisoformat(
                str(ts).replace("Z", "+00:00")
            )
            if hasattr(event_start, "tzinfo") and event_start.tzinfo is None:
                event_start = event_start.replace(tzinfo=timezone.utc)
            lead_days = (event_start - uploaded).days
        except Exception:
            pass
        lead_time_data.append({
            "label": uploaded.strftime("%b %d %H:%M"),
            "lead_days": lead_days,
            "value": round(act.value or 0, 2),
            "status": act.status,
            "probability": round((act.probability or 0) * 100, 1) if act.probability else None,
        })

    lead_time_json = json.dumps(lead_time_data)

    # Return period context from CHIRPS historical data
    threshold_rp: float | None = None
    threshold_rp_label: str = ""
    threshold_rp_color: str = "#6b7280"
    activation_rps: dict[int, dict] = {}
    rl_row = await db.scalar(
        select(ReturnLevel).where(ReturnLevel.variable == trigger.variable)
    )
    if rl_row and rl_row.gev_shape is not None:
        rp = return_period_for_value(
            rl_row.gev_shape, rl_row.gev_loc, rl_row.gev_scale, trigger.threshold
        )
        threshold_rp = rp
        threshold_rp_label = rp_label(rp)
        threshold_rp_color = rp_color(rp)
        for a in activations:
            if a.value is not None:
                arp = return_period_for_value(
                    rl_row.gev_shape, rl_row.gev_loc, rl_row.gev_scale, a.value
                )
                activation_rps[a.id] = {
                    "label": rp_label(arp),
                    "color": rp_color(arp),
                }

    return templates.TemplateResponse(
    request,
    "trigger_detail.html",
    {"user": user, "trigger": trigger,
         "activations": activations, "OPERATOR_SYMBOLS": OPERATOR_SYMBOLS,
         "VARIABLE_LABELS": VARIABLE_LABELS,
         "impacts_by_activation": impacts_by_activation,
         "validated_count": validated_count,
         "comments_by_activation": comments_by_activation,
         "lead_time_json": lead_time_json,
         "lead_time_data_exists": len(lead_time_data) > 0,
         "threshold_rp_label": threshold_rp_label,
         "threshold_rp_color": threshold_rp_color,
         "activation_rps": activation_rps},
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
        request, "trigger_form.html", _trigger_form_ctx(request, user, trigger)
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
    cond2_enabled: Optional[str] = Form(None),
    cond2_variable: str = Form(""),
    cond2_operator: str = Form(""),
    cond2_threshold: str = Form(""),
    logic_op: str = Form("and"),
    scope_polygon: str = Form(""),
    prob_enabled: Optional[str] = Form(None),
    probability_threshold: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = result.scalar_one_or_none()
    if not trigger:
        return RedirectResponse("/triggers")

    threshold_val, extras, error = _validate_trigger(
        name, hazard_type, variable, operator, threshold, is_active,
        scope_enabled == "on", scope_lat_min, scope_lat_max, scope_lon_min, scope_lon_max,
        cond2_enabled == "on", cond2_variable, cond2_operator, cond2_threshold, logic_op, scope_polygon,
        prob_enabled == "on", probability_threshold,
    )
    if error:
        stub = SimpleNamespace(
            id=trigger_id, name=name, hazard_type=hazard_type, variable=variable,
            operator=operator, threshold=threshold, is_active=(is_active == "on"),
            scope_lat_min=scope_lat_min, scope_lat_max=scope_lat_max,
            scope_lon_min=scope_lon_min, scope_lon_max=scope_lon_max,
            condition_2_variable=cond2_variable, condition_2_operator=cond2_operator,
            condition_2_threshold=cond2_threshold, logic_op=logic_op, scope_polygon=scope_polygon,
            probability_threshold=probability_threshold,
        )
        return templates.TemplateResponse(
            request, "trigger_form.html", _trigger_form_ctx(request, user, stub,
                error=error, scope_enabled=(scope_enabled == "on"),
                cond2_enabled=(cond2_enabled == "on"),
                prob_enabled=(prob_enabled == "on"))
        )

    trigger.name = name
    trigger.hazard_type = hazard_type
    trigger.variable = variable
    trigger.operator = operator
    trigger.threshold = threshold_val
    trigger.is_active = is_active == "on"
    for k, v in (extras or {}).items():
        setattr(trigger, k, v)
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


@router.get("/activations/{activation_id}/sitrep", response_class=HTMLResponse)
async def activation_sitrep(
    activation_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    from sqlalchemy import and_, or_

    result = await db.execute(
        select(TriggerActivation).where(TriggerActivation.id == activation_id)
    )
    activation = result.scalar_one_or_none()
    if not activation:
        return RedirectResponse("/triggers")

    trigger = activation.trigger
    forecast = activation.forecast

    # Impacts directly linked to this activation
    linked_result = await db.execute(
        select(ImpactRecord)
        .where(ImpactRecord.trigger_activation_id == activation_id)
        .order_by(ImpactRecord.event_date)
    )
    linked_impacts = linked_result.scalars().all()

    # Nearby unlinked impacts — same hazard type within ±30 days
    nearby_impacts = []
    if trigger and trigger.hazard_type:
        act_date = activation.triggered_at.date()
        ws = act_date - timedelta(days=30)
        we = act_date + timedelta(days=30)
        linked_ids = [imp.id for imp in linked_impacts]
        nearby_stmt = (
            select(ImpactRecord)
            .where(
                ImpactRecord.hazard_type == trigger.hazard_type,
                ImpactRecord.event_date >= ws,
                ImpactRecord.event_date <= we,
            )
            .order_by(ImpactRecord.event_date)
        )
        if linked_ids:
            nearby_stmt = nearby_stmt.where(~ImpactRecord.id.in_(linked_ids))
        nearby_impacts = (await db.execute(nearby_stmt)).scalars().all()

    # Pre-format rule and deviation
    op_sym = OPERATOR_SYMBOLS.get(trigger.operator, trigger.operator) if trigger else "?"
    var_label = VARIABLE_LABELS.get(trigger.variable, trigger.variable) if trigger else "?"
    _unit = "" if (trigger and trigger.variable in SPI_VARIABLES) else " mm"
    rule_str = f"{var_label} {op_sym} {trigger.threshold}{_unit}" if trigger else "—"
    if trigger and trigger.threshold:
        dev_mm = round(activation.value - trigger.threshold, 2)
        dev_pct = round(dev_mm / trigger.threshold * 100, 1)
    else:
        dev_mm = dev_pct = None

    # Impact summary totals
    def _sum(items, attr):
        vals = [getattr(i, attr) for i in items if getattr(i, attr) is not None]
        return sum(vals) if vals else None

    total_affected  = _sum(linked_impacts, "affected_population")
    total_casualties = _sum(linked_impacts, "casualties")
    total_displaced  = _sum(linked_impacts, "displaced")
    total_damage     = _sum(linked_impacts, "damage_usd")

    # Map points (linked impacts with coordinates)
    map_points = json.dumps([
        {"id": imp.id, "lat": imp.lat, "lon": imp.lon,
         "event_name": imp.event_name, "country": imp.country or "",
         "event_date": str(imp.event_date)}
        for imp in linked_impacts if imp.lat is not None and imp.lon is not None
    ])
    has_map = bool(forecast and forecast.lat_min is not None) or bool(
        any(imp.lat is not None for imp in linked_impacts)
    )

    return templates.TemplateResponse(
    request,
    "sitrep.html",
    {
            "user": user,
            "activation": activation,
            "trigger": trigger,
            "forecast": forecast,
            "linked_impacts": linked_impacts,
            "nearby_impacts": nearby_impacts,
            "rule_str": rule_str,
            "dev_mm": dev_mm,
            "dev_pct": dev_pct,
            "total_affected": total_affected,
            "total_casualties": total_casualties,
            "total_displaced": total_displaced,
            "total_damage": total_damage,
            "map_points": map_points,
            "has_map": has_map,
            "OPERATOR_SYMBOLS": OPERATOR_SYMBOLS,
            "VARIABLE_LABELS": VARIABLE_LABELS,
        },
)


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

        # Notify subscribers
        subs_result = await db.execute(
            select(User.email)
            .join(TriggerSubscription, TriggerSubscription.user_id == User.id)
            .where(
                TriggerSubscription.trigger_id == activation.trigger_id,
                User.is_active == True,  # noqa: E712
                User.role != "admin",
            )
        )
        sub_emails = [row[0] for row in subs_result.all()]
        if sub_emails:
            enqueue(
                send_acknowledgement_emails(sub_emails, activation, activation.trigger, notes or "")
            )

        return RedirectResponse(f"/triggers/{activation.trigger_id}", status_code=303)
    return RedirectResponse("/triggers", status_code=303)


_VALID_VERDICTS = {"yes", "partial", "no"}


@router.post("/activations/{activation_id}/verify")
async def verify_activation_impacts(
    activation_id: int,
    request: Request,
    verdict: str = Form(...),
    impact_notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if verdict not in _VALID_VERDICTS:
        return RedirectResponse(f"/triggers/activations/{activation_id}/sitrep", status_code=303)

    result = await db.execute(
        select(TriggerActivation).where(TriggerActivation.id == activation_id)
    )
    activation = result.scalar_one_or_none()
    if activation:
        activation.impact_verdict = verdict
        activation.impact_notes = impact_notes.strip() or None
        activation.verified_at = datetime.now(timezone.utc)
        await db.commit()
        await log_action(
            db, user.id, "trigger.impact_verified",
            f"Impact verdict '{verdict}' for activation {activation_id} ({activation.trigger.name})",
        )

    return RedirectResponse(f"/triggers/activations/{activation_id}/sitrep", status_code=303)


@router.post("/activations/{activation_id}/comments")
async def add_activation_comment(
    activation_id: int,
    request: Request,
    text: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(
        select(TriggerActivation).where(TriggerActivation.id == activation_id)
    )
    activation = result.scalar_one_or_none()
    if not activation:
        return RedirectResponse("/triggers", status_code=303)

    if text.strip():
        db.add(ActivationComment(
            activation_id=activation_id,
            user_id=user.id,
            text=text.strip(),
        ))
        await db.commit()
        await log_action(db, user.id, "trigger.comment",
                         f"Added comment to activation {activation_id}")

    return RedirectResponse(f"/triggers/{activation.trigger_id}", status_code=303)


@router.post("/activations/{activation_id}/comments/{comment_id}/delete")
async def delete_activation_comment(
    activation_id: int,
    comment_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(
        select(ActivationComment).where(ActivationComment.id == comment_id)
    )
    comment = result.scalar_one_or_none()
    if comment and (comment.user_id == user.id or user.role == "admin"):
        result2 = await db.execute(
            select(TriggerActivation).where(TriggerActivation.id == activation_id)
        )
        activation = result2.scalar_one_or_none()
        await db.delete(comment)
        await db.commit()
        if activation:
            return RedirectResponse(f"/triggers/{activation.trigger_id}", status_code=303)
    return RedirectResponse("/triggers", status_code=303)


@router.post("/activations/bulk-acknowledge")
async def bulk_acknowledge(
    request: Request,
    activation_ids: list[int] = Form(default=[]),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/login")

    if not activation_ids:
        return RedirectResponse("/triggers", status_code=303)

    result = await db.execute(
        select(TriggerActivation).where(
            TriggerActivation.id.in_(activation_ids),
            TriggerActivation.status == "active",
        )
    )
    activations = result.scalars().all()
    now = datetime.now(timezone.utc)
    for act in activations:
        act.status = "acknowledged"
        act.acknowledged_at = now
        act.notes = notes or None

    await db.commit()
    await log_action(db, user.id, "trigger.bulk_acknowledge",
                     f"Bulk-acknowledged {len(activations)} activation(s)")
    return RedirectResponse("/triggers", status_code=303)
