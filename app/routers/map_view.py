import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.forecast import ForecastUpload
from app.models.glofas import GlofasRecord
from app.models.impact import ImpactRecord
from app.models.trigger import Trigger, TriggerActivation

router = APIRouter(prefix="/map")
templates = Jinja2Templates(directory="app/templates")

_HAZARD_COLOR = {
    "flood":   "#3b82f6",
    "storm":   "#8b5cf6",
    "drought": "#f59e0b",
    "cyclone": "#06b6d4",
    "other":   "#6b7280",
}


def _bbox_ring(lon_min, lat_min, lon_max, lat_max):
    return [
        [lon_min, lat_min], [lon_max, lat_min],
        [lon_max, lat_max], [lon_min, lat_max],
        [lon_min, lat_min],
    ]


@router.get("", response_class=HTMLResponse)
async def map_view(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    active_count = len((await db.execute(
        select(TriggerActivation).where(TriggerActivation.status == "active")
    )).scalars().all())

    latest_fc = (await db.execute(
        select(ForecastUpload)
        .where(ForecastUpload.geojson.isnot(None))
        .order_by(ForecastUpload.uploaded_at.desc())
        .limit(1)
    )).scalars().first()

    latest_glofas = (await db.execute(
        select(GlofasRecord)
        .where(GlofasRecord.geojson.isnot(None))
        .order_by(GlofasRecord.forecast_date.desc())
        .limit(1)
    )).scalars().first()

    impact_count = len((await db.execute(
        select(ImpactRecord)
        .where(ImpactRecord.lat.isnot(None))
    )).scalars().all())

    return templates.TemplateResponse(request, "map_view.html", {
        "user": user,
        "active_count": active_count,
        "latest_fc_date": latest_fc.uploaded_at.strftime("%Y-%m-%d") if latest_fc else None,
        "latest_fc_source": latest_fc.source if latest_fc else None,
        "has_glofas": latest_glofas is not None,
        "glofas_date": latest_glofas.forecast_date.isoformat() if latest_glofas else None,
        "impact_count": impact_count,
    })


@router.get("/layers/triggers")
async def layer_triggers(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    triggers = (await db.execute(
        select(Trigger).where(Trigger.is_active == True)
    )).scalars().all()

    active_acts = {
        a.trigger_id: a for a in (await db.execute(
            select(TriggerActivation).where(TriggerActivation.status == "active")
        )).scalars().all()
    }

    features = []
    for t in triggers:
        ring = None
        if t.scope_polygon:
            try:
                ring = json.loads(t.scope_polygon)
            except Exception:
                pass
        elif all(v is not None for v in [t.scope_lat_min, t.scope_lat_max,
                                          t.scope_lon_min, t.scope_lon_max]):
            ring = _bbox_ring(t.scope_lon_min, t.scope_lat_min,
                              t.scope_lon_max, t.scope_lat_max)
        if ring is None:
            continue

        act = active_acts.get(t.id)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {
                "id": t.id,
                "name": t.name,
                "hazard_type": t.hazard_type or "other",
                "color": _HAZARD_COLOR.get(t.hazard_type or "other", "#6b7280"),
                "variable": t.variable,
                "threshold": t.threshold,
                "response_plan": t.response_plan or "",
                "is_alert": act is not None,
                "activation_id": act.id if act else None,
                "value": act.value if act else None,
                "triggered_at": act.triggered_at.isoformat() if act and act.triggered_at else None,
            },
        })

    return JSONResponse({"type": "FeatureCollection", "features": features})


@router.get("/layers/rainfall")
async def layer_rainfall(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    fc = (await db.execute(
        select(ForecastUpload)
        .where(ForecastUpload.geojson.isnot(None))
        .where(or_(ForecastUpload.variable == "tp", ForecastUpload.variable.is_(None)))
        .order_by(ForecastUpload.uploaded_at.desc())
        .limit(1)
    )).scalars().first()

    if not fc:
        return JSONResponse({"type": "FeatureCollection", "features": [], "meta": None})

    try:
        geojson = json.loads(fc.geojson)
    except Exception:
        return JSONResponse({"type": "FeatureCollection", "features": [], "meta": None})

    geojson["meta"] = {
        "forecast_id": fc.id,
        "source": fc.source,
        "uploaded_at": fc.uploaded_at.isoformat(),
        "precip_mean": fc.precip_mean,
        "precip_max": fc.precip_max,
    }
    return JSONResponse(geojson)


@router.get("/layers/glofas")
async def layer_glofas(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    rec = (await db.execute(
        select(GlofasRecord)
        .where(GlofasRecord.geojson.isnot(None))
        .order_by(GlofasRecord.forecast_date.desc())
        .limit(1)
    )).scalars().first()

    if not rec:
        return JSONResponse({"type": "FeatureCollection", "features": [], "meta": None})

    try:
        geojson = json.loads(rec.geojson)
    except Exception:
        return JSONResponse({"type": "FeatureCollection", "features": [], "meta": None})

    geojson["meta"] = {
        "forecast_date": rec.forecast_date.isoformat() if rec.forecast_date else None,
        "discharge_mean": rec.discharge_mean,
        "discharge_max": rec.discharge_max,
        "lead_days": rec.lead_days,
    }
    return JSONResponse(geojson)


@router.get("/layers/impacts")
async def layer_impacts(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    impacts = (await db.execute(
        select(ImpactRecord)
        .where(ImpactRecord.lat.isnot(None))
        .where(ImpactRecord.lon.isnot(None))
        .order_by(ImpactRecord.event_date.desc())
        .limit(300)
    )).scalars().all()

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [i.lon, i.lat]},
            "properties": {
                "id": i.id,
                "event_name": i.event_name or "Unnamed event",
                "hazard_type": i.hazard_type or "other",
                "color": _HAZARD_COLOR.get(i.hazard_type or "other", "#6b7280"),
                "event_date": i.event_date.isoformat() if i.event_date else None,
                "country": i.country or "",
                "region": i.region or "",
                "affected_population": i.affected_population or 0,
                "casualties": i.casualties or 0,
                "displaced": i.displaced or 0,
            },
        }
        for i in impacts
    ]

    return JSONResponse({"type": "FeatureCollection", "features": features})
