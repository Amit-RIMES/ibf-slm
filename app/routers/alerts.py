import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.trigger import Trigger, TriggerActivation

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
    """Return (heatmap_points, total_count) for activations in the window.

    Each point is [lat, lon, intensity] where intensity is the normalised count
    at that trigger's centroid.  Activations on triggers with no geographic scope
    are skipped.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)) if days > 0 else None

    q = (
        select(TriggerActivation, Trigger)
        .join(Trigger, TriggerActivation.trigger_id == Trigger.id)
        .order_by(TriggerActivation.triggered_at.desc())
    )
    if cutoff:
        q = q.where(TriggerActivation.triggered_at >= cutoff)

    result = await db.execute(q)
    rows = result.all()

    # Accumulate counts per centroid (rounded to 2dp to merge near-identical points)
    centroid_counts: dict[tuple, int] = {}
    for act, trig in rows:
        lat = lon = None
        if trig.scope_polygon:
            try:
                ring = json.loads(trig.scope_polygon)
                lons = [p[0] for p in ring]
                lats = [p[1] for p in ring]
                lat = sum(lats) / len(lats)
                lon = sum(lons) / len(lons)
            except Exception:
                pass
        elif trig.scope_lat_min is not None and trig.scope_lat_max is not None:
            lat = (trig.scope_lat_min + trig.scope_lat_max) / 2
            lon = (trig.scope_lon_min + trig.scope_lon_max) / 2

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


@router.get("/status", response_class=HTMLResponse)
async def public_status(request: Request, db: AsyncSession = Depends(get_db)):
    alerts_json, activations = await _get_active_alerts(db)

    # Public view: strip internal fields
    public_alerts = [
        {
            "hazard_type": a["hazard_type"],
            "triggered_at": a["triggered_at"],
            "lat_min": a["lat_min"],
            "lat_max": a["lat_max"],
            "lon_min": a["lon_min"],
            "lon_max": a["lon_max"],
        }
        for a in alerts_json
    ]

    return templates.TemplateResponse(
    request,
    "status.html",
    {
            "alert_count": len(public_alerts),
            "alerts_json": json.dumps(public_alerts),
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        },
)
