import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.geo_risk import (
    compute_country_risk, get_regional_advisories, has_boundary_geometry, microstate_centroid,
)
from app.core.i18n import SUPPORTED_LANGUAGES, get_public_copy
from app.models.alert_recipient import AlertRecipient
from app.models.forecast import ForecastUpload
from app.models.trigger import Trigger, TriggerActivation
from app.routers.forecasts import COUNTRY_NAMES

_HAZARD_COLORS = {
    "flood": "#3b82f6", "storm": "#8b5cf6", "drought": "#f59e0b",
    "landslide": "#10b981", "heatwave": "#ef4444", "cyclone": "#0ea5e9", "other": "#6b7280",
}

_RISK_COLORS = {"low": "#6f9a3c", "moderate": "#d9a62e", "high": "#d97b29", "extreme": "#c0392b"}
_RISK_ORDER = {"extreme": 3, "high": 2, "moderate": 1, "low": 0}
_COUNTRY_ISO2_BY_NAME = {name: iso2 for iso2, name in COUNTRY_NAMES.items()}

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


async def _get_active_alerts(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(TriggerActivation)
        .where(TriggerActivation.status == "active")
        .order_by(TriggerActivation.triggered_at.desc())
    )
    activations = result.scalars().all()
    from app.models.trigger import OPERATOR_SYMBOLS
    from app.routers.triggers import VARIABLE_LABELS
    alerts = []
    for a in activations:
        if not a.forecast:
            continue
        fc = a.forecast
        t = a.trigger
        alerts.append({
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
    return alerts, activations


async def _build_heatmap(db: AsyncSession, days: int) -> tuple[list, int]:
    """Return (heatmap_points, total_count) for activations in the window."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)) if days > 0 else None

    q = (
        select(TriggerActivation, Trigger, ForecastUpload)
        .join(Trigger, TriggerActivation.trigger_id == Trigger.id)
        .outerjoin(ForecastUpload, TriggerActivation.forecast_id == ForecastUpload.id)
        .order_by(TriggerActivation.triggered_at.desc())
    )
    if cutoff:
        q = q.where(TriggerActivation.triggered_at >= cutoff)

    result = await db.execute(q)
    rows = result.all()

    centroid_counts: dict[tuple, int] = {}
    for act, trig, fc in rows:
        lat = lon = None
        # 1. Prefer trigger's own scope polygon
        if trig.scope_polygon:
            try:
                ring = json.loads(trig.scope_polygon)
                lons_r = [p[0] for p in ring]
                lats_r = [p[1] for p in ring]
                lat = sum(lats_r) / len(lats_r)
                lon = sum(lons_r) / len(lons_r)
            except Exception:
                pass
        # 2. Trigger bbox scope
        if lat is None and trig.scope_lat_min is not None and trig.scope_lat_max is not None:
            lat = (trig.scope_lat_min + trig.scope_lat_max) / 2
            lon = (trig.scope_lon_min + trig.scope_lon_max) / 2
        # 3. Fall back to the forecast bbox centroid
        if lat is None and fc is not None and fc.lat_min is not None:
            lat = (fc.lat_min + fc.lat_max) / 2
            lon = (fc.lon_min + fc.lon_max) / 2

        if lat is None:
            continue

        key = (round(lat, 2), round(lon, 2))
        centroid_counts[key] = centroid_counts.get(key, 0) + 1

    if not centroid_counts:
        return [], len(rows)

    max_count = max(centroid_counts.values())
    points = [
        [lat, lon, round(count / max_count, 3)]
        for (lat, lon), count in centroid_counts.items()
    ]
    return points, len(rows)


@router.get("/alerts", response_class=HTMLResponse)
async def alert_map(
    request: Request,
    heatmap_days: int = 90,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    alerts_json, activations = await _get_active_alerts(db)
    heatmap_points, heatmap_total = await _build_heatmap(db, heatmap_days)

    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            "user": user,
            "activations": activations,
            "alerts_json": json.dumps(alerts_json),
            "heatmap_json": json.dumps(heatmap_points),
            "heatmap_total": heatmap_total,
            "heatmap_days": heatmap_days,
        },
    )


@router.get("/alerts/timeline", response_class=HTMLResponse)
async def alert_timeline(
    request: Request,
    days: int = 30,
    hazard: str = "",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    days = max(7, min(365, days))
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    window_span = (now - window_start).total_seconds()

    stmt = (
        select(TriggerActivation)
        .join(Trigger, TriggerActivation.trigger_id == Trigger.id)
        .where(TriggerActivation.triggered_at >= window_start)
        .order_by(TriggerActivation.triggered_at)
    )
    if hazard:
        stmt = stmt.where(Trigger.hazard_type == hazard)

    all_acts = (await db.execute(stmt)).scalars().all()

    # Group by trigger, build bar data
    by_trigger: dict[int, dict] = {}
    for act in all_acts:
        tid = act.trigger_id
        if tid not in by_trigger:
            t = act.trigger
            by_trigger[tid] = {
                "trigger_id": tid,
                "name": t.name,
                "hazard_type": t.hazard_type or "other",
                "color": _HAZARD_COLORS.get(t.hazard_type or "other", "#6b7280"),
                "bars": [],
            }

        at = act.triggered_at
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)

        ack = act.acknowledged_at
        if ack and ack.tzinfo is None:
            ack = ack.replace(tzinfo=timezone.utc)
        bar_end = min(ack if ack else now, now)
        bar_start = max(at, window_start)

        if bar_start >= now or bar_end <= window_start:
            continue

        left_pct = (bar_start - window_start).total_seconds() / window_span * 100
        width_pct = max((bar_end - bar_start).total_seconds() / window_span * 100, 0.4)

        if ack:
            dur_h = round((ack - at).total_seconds() / 3600, 1)
            end_label = ack.strftime("%b %d %H:%M")
        else:
            dur_h = None
            end_label = "ongoing"

        by_trigger[tid]["bars"].append({
            "id": act.id,
            "left_pct": round(left_pct, 2),
            "width_pct": round(width_pct, 2),
            "status": act.status,
            "value": round(act.value, 2),
            "start_label": at.strftime("%b %d %H:%M"),
            "end_label": end_label,
            "duration_h": dur_h,
        })

    rows = list(by_trigger.values())
    # Sort: most recent activity first
    rows.sort(
        key=lambda r: max((b["left_pct"] + b["width_pct"] for b in r["bars"]), default=0),
        reverse=True,
    )

    # Date ticks (aim for ~6 evenly spaced)
    tick_every = max(1, days // 6)
    ticks = []
    tick_dt = window_start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    while tick_dt <= now:
        pct = (tick_dt - window_start).total_seconds() / window_span * 100
        if 0 <= pct <= 100:
            ticks.append({"label": tick_dt.strftime("%b %d"), "pct": round(pct, 2)})
        tick_dt += timedelta(days=tick_every)

    # Unique hazard types for filter dropdown
    hazard_types_r = await db.execute(
        select(Trigger.hazard_type).distinct().where(Trigger.hazard_type.isnot(None))
    )
    hazard_types = sorted(r[0] for r in hazard_types_r.all())

    timeline_json = json.dumps({
        "rows": rows,
        "ticks": ticks,
        "today_pct": 100.0,
    })

    return templates.TemplateResponse(
        request,
        "alert_timeline.html",
        {
            "user": user,
            "timeline_json": timeline_json,
            "days": days,
            "hazard": hazard,
            "hazard_types": hazard_types,
            "total_activations": len(all_acts),
            "total_triggers": len(rows),
        },
    )


@router.get("/status", response_class=HTMLResponse)
async def public_status(
    request: Request,
    country: str = "",
    lang: str = "en",
    db: AsyncSession = Depends(get_db),
):
    if lang not in SUPPORTED_LANGUAGES:
        lang = "en"
    copy = get_public_copy(lang)

    country_list = sorted(COUNTRY_NAMES.items(), key=lambda kv: kv[1])  # [(iso2, name), ...] by name

    country_name = COUNTRY_NAMES.get(country)
    if not country_name:
        # No (or unrecognised) country selected — picker landing state.
        return templates.TemplateResponse(
            request,
            "status.html",
            {
                "has_country": False,
                "country_list": country_list,
                "lang": lang,
                "supported_languages": SUPPORTED_LANGUAGES,
                "copy": copy,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            },
        )

    risk = await compute_country_risk(db, country, country_name)
    regional = await get_regional_advisories(db)

    hazard_cards = []
    for hazard_type, info in risk.items():
        hazard_cards.append({
            "hazard_type": hazard_type,
            "hazard_name": copy["hazard_names"].get(hazard_type, hazard_type.title()),
            "level": info["level"],
            "level_label": copy["risk_labels"][info["level"]],
            "color": _RISK_COLORS[info["level"]],
            "status_text": copy["status_text"].get(hazard_type, {}).get(info["level"], ""),
            "tips": copy["action_tips"].get(hazard_type, []),
        })
    hazard_cards.sort(key=lambda c: _RISK_ORDER[c["level"]], reverse=True)

    hero = hazard_cards[0] if hazard_cards and _RISK_ORDER[hazard_cards[0]["level"]] > 0 else None
    other_cards = hazard_cards[1:] if hero else hazard_cards

    for adv in regional:
        adv["hazard_name"] = copy["hazard_names"].get(adv["hazard_type"], adv["hazard_type"].title())

    has_geometry = has_boundary_geometry(country_name)
    fallback_centroid = None if has_geometry else microstate_centroid(country_name)

    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "has_country": True,
            "country_list": country_list,
            "country_iso2": country,
            "country_name": country_name,
            "lang": lang,
            "supported_languages": SUPPORTED_LANGUAGES,
            "copy": copy,
            "hero": hero,
            "hazard_cards": other_cards,
            "regional_advisories": regional,
            "has_geometry": has_geometry,
            "fallback_centroid_json": json.dumps(fallback_centroid) if fallback_centroid else "null",
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        },
    )


# ── Alert Recipients (external email subscribers) ─────────────────────────────

_FORBIDDEN = HTMLResponse(
    "<h1 style='font-family:system-ui;margin:3rem auto;max-width:400px'>403 — Admin access required</h1>",
    status_code=403,
)


@router.get("/alerts/recipients", response_class=HTMLResponse)
async def alert_recipients(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    recipients_r = await db.execute(
        select(AlertRecipient).order_by(AlertRecipient.created_at)
    )
    recipients = recipients_r.scalars().all()

    active_count = sum(1 for r in recipients if r.is_active)

    return templates.TemplateResponse(
        request,
        "alert_recipients.html",
        {"user": user, "recipients": recipients, "active_count": active_count},
    )


@router.post("/alerts/recipients/add", response_class=HTMLResponse)
async def alert_recipient_add(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    phone: str = Form(""),
    whatsapp_enabled: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    email = email.strip().lower()
    if email:
        existing = await db.scalar(
            select(AlertRecipient).where(AlertRecipient.email == email)
        )
        if not existing:
            db.add(AlertRecipient(
                email=email,
                name=name.strip(),
                phone=phone.strip() or None,
                whatsapp_enabled=bool(whatsapp_enabled),
            ))
            await db.commit()

    return RedirectResponse("/alerts/recipients", status_code=303)


@router.post("/alerts/recipients/{rec_id}/phone", response_class=HTMLResponse)
async def alert_recipient_update_phone(
    rec_id: int,
    request: Request,
    phone: str = Form(""),
    whatsapp_enabled: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    rec = await db.scalar(select(AlertRecipient).where(AlertRecipient.id == rec_id))
    if rec:
        rec.phone = phone.strip() or None
        rec.whatsapp_enabled = bool(whatsapp_enabled)
        await db.commit()

    return RedirectResponse("/alerts/recipients", status_code=303)


@router.post("/alerts/recipients/{rec_id}/toggle", response_class=HTMLResponse)
async def alert_recipient_toggle(
    rec_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    rec = await db.scalar(select(AlertRecipient).where(AlertRecipient.id == rec_id))
    if rec:
        rec.is_active = not rec.is_active
        await db.commit()

    return RedirectResponse("/alerts/recipients", status_code=303)


@router.post("/alerts/recipients/{rec_id}/delete", response_class=HTMLResponse)
async def alert_recipient_delete(
    rec_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    rec = await db.scalar(select(AlertRecipient).where(AlertRecipient.id == rec_id))
    if rec:
        await db.delete(rec)
        await db.commit()

    return RedirectResponse("/alerts/recipients", status_code=303)
