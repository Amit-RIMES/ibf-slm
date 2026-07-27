import io
import json
import math
import os

import numpy as np
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.scope import allowed_country_names, allowed_sources, country_condition
from app.models.forecast import ForecastUpload
from app.models.glofas import GlofasRecord
from app.models.impact import ImpactRecord
from app.models.observed_rainfall import ObservedRainfall
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

    scope = allowed_sources(user)
    scope_names = allowed_country_names(user)

    active_count = len((await db.execute(
        select(TriggerActivation).where(TriggerActivation.status == "active")
    )).scalars().all())

    latest_fc_stmt = select(ForecastUpload).where(ForecastUpload.geojson.isnot(None))
    if scope is not None:
        latest_fc_stmt = latest_fc_stmt.where(ForecastUpload.source.in_(scope))
    latest_fc = (await db.execute(
        latest_fc_stmt.order_by(ForecastUpload.uploaded_at.desc()).limit(1)
    )).scalars().first()

    latest_glofas = (await db.execute(
        select(GlofasRecord)
        .where(GlofasRecord.geojson.isnot(None))
        .order_by(GlofasRecord.forecast_date.desc())
        .limit(1)
    )).scalars().first()

    impact_stmt = select(ImpactRecord).where(ImpactRecord.lat.isnot(None))
    impact_scope_cond = country_condition(ImpactRecord.country, scope_names)
    if impact_scope_cond is not None:
        impact_stmt = impact_stmt.where(impact_scope_cond)
    impact_count = len((await db.execute(impact_stmt)).scalars().all())

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

    rainfall_stmt = (
        select(ForecastUpload)
        .where(ForecastUpload.geojson.isnot(None))
        .where(or_(ForecastUpload.variable == "tp", ForecastUpload.variable.is_(None)))
    )
    scope = allowed_sources(user)
    if scope is not None:
        rainfall_stmt = rainfall_stmt.where(ForecastUpload.source.in_(scope))
    fc = (await db.execute(
        rainfall_stmt.order_by(ForecastUpload.uploaded_at.desc()).limit(1)
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

    impacts_stmt = (
        select(ImpactRecord)
        .where(ImpactRecord.lat.isnot(None))
        .where(ImpactRecord.lon.isnot(None))
    )
    scope_cond = country_condition(ImpactRecord.country, allowed_country_names(user))
    if scope_cond is not None:
        impacts_stmt = impacts_stmt.where(scope_cond)
    impacts = (await db.execute(
        impacts_stmt.order_by(ImpactRecord.event_date.desc()).limit(300)
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


# ── Precipitation colormap (purple→blue→cyan→green→yellow→orange→red) ─────────

def _precip_rgba(value: float, vmax: float) -> tuple[int, int, int, int]:
    """Map a precipitation value to an RGBA tuple using a met-standard color ramp."""
    if vmax <= 0 or value <= 0:
        return (0, 0, 0, 0)  # transparent

    # Log-scale normalise so low values still get color
    import math as _m
    t = _m.log1p(value) / _m.log1p(vmax)
    t = max(0.0, min(1.0, t))

    # Color stops: (t, R, G, B, A)
    stops = [
        (0.00, 70,  0,  130, 0),    # transparent at zero
        (0.05, 70,  0,  130, 160),  # indigo
        (0.20, 30,  80,  220, 200), # blue
        (0.40, 0,  180,  220, 210), # cyan
        (0.60, 0,  200,   80, 220), # green
        (0.75, 240, 230,   0, 230), # yellow
        (0.88, 255, 130,   0, 240), # orange
        (1.00, 220,   0,   0, 250), # red
    ]

    for i in range(len(stops) - 1):
        t0, r0, g0, b0, a0 = stops[i]
        t1, r1, g1, b1, a1 = stops[i + 1]
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            r = int(r0 + f * (r1 - r0))
            g = int(g0 + f * (g1 - g0))
            b = int(b0 + f * (b1 - b0))
            a = int(a0 + f * (a1 - a0))
            return (r, g, b, a)

    return (220, 0, 0, 250)


def _discharge_rgba(value: float, vmax: float) -> tuple[int, int, int, int]:
    """River discharge colormap: transparent → light cyan → blue → deep navy."""
    if vmax <= 0 or value <= 0:
        return (0, 0, 0, 0)

    import math as _m
    t = _m.log1p(value) / _m.log1p(vmax)
    t = max(0.0, min(1.0, t))

    # Color stops (t, R, G, B, A)
    stops = [
        (0.00, 180, 235, 255,   0),   # transparent
        (0.08, 180, 235, 255, 160),   # very light cyan
        (0.25,  80, 200, 240, 200),   # cyan
        (0.45,  20, 130, 210, 215),   # sky blue
        (0.65,  10,  70, 160, 225),   # medium blue
        (0.82,   5,  35, 110, 235),   # deep blue
        (1.00,   2,  12,  55, 245),   # near-black navy
    ]

    for i in range(len(stops) - 1):
        t0, r0, g0, b0, a0 = stops[i]
        t1, r1, g1, b1, a1 = stops[i + 1]
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return (
                int(r0 + f * (r1 - r0)),
                int(g0 + f * (g1 - g0)),
                int(b0 + f * (b1 - b0)),
                int(a0 + f * (a1 - a0)),
            )

    return (2, 12, 55, 245)


def _build_raster_png(lats: np.ndarray, lons: np.ndarray, grid: np.ndarray,
                      scale: int = 2, rgba_fn=None) -> tuple[bytes, float, float, float, float]:
    """Interpolate the forecast grid and render as a transparent RGBA PNG.
    Returns (png_bytes, lat_min, lat_max, lon_min, lon_max).
    rgba_fn(value, vmax) → (R,G,B,A); defaults to _precip_rgba.
    """
    from scipy.interpolate import RegularGridInterpolator
    from scipy.ndimage import distance_transform_edt
    from PIL import Image

    if rgba_fn is None:
        rgba_fn = _precip_rgba

    # 2D nearest-neighbour NaN fill — avoids horizontal/vertical stripes
    nan_mask = np.isnan(grid)
    grid_filled = np.copy(grid)
    if nan_mask.any():
        _, idx = distance_transform_edt(nan_mask, return_distances=True, return_indices=True)
        grid_filled = grid[tuple(idx)]

    h, w = len(lats), len(lons)
    lat_fine = np.linspace(lats[0], lats[-1], h * scale)
    lon_fine = np.linspace(lons[0], lons[-1], w * scale)

    interp_fn = RegularGridInterpolator(
        (lats, lons), grid_filled, method="linear", bounds_error=False, fill_value=0.0
    )
    lon_g, lat_g = np.meshgrid(lon_fine, lat_fine)
    values = interp_fn((lat_g, lon_g))

    vmax = float(np.nanmax(values)) if np.nanmax(values) > 0 else 1.0

    rgba = np.zeros((len(lat_fine), len(lon_fine), 4), dtype=np.uint8)
    for i in range(len(lat_fine)):
        for j in range(len(lon_fine)):
            rgba[i, j] = rgba_fn(float(values[i, j]), vmax)

    rgba = np.flipud(rgba)

    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return (
        buf.read(),
        float(lats.min()), float(lats.max()),
        float(lons.min()), float(lons.max()),
    )


@router.get("/layers/rainfall-raster")
async def layer_rainfall_raster(
    request: Request,
    lead: str = Query(default="total", description="Lead time: total|d1_5|d6_10|d11_15"),
    fc_id: int = Query(default=None, description="Specific forecast ID; omit for latest"),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    scope = allowed_sources(user)
    if fc_id is not None:
        fc = (await db.execute(
            select(ForecastUpload).where(ForecastUpload.id == fc_id)
        )).scalars().first()
    else:
        rr_stmt = (
            select(ForecastUpload)
            .where(ForecastUpload.geojson.isnot(None))
            .where(or_(ForecastUpload.variable == "tp", ForecastUpload.variable.is_(None)))
        )
        if scope is not None:
            rr_stmt = rr_stmt.where(ForecastUpload.source.in_(scope))
        fc = (await db.execute(
            rr_stmt.order_by(ForecastUpload.uploaded_at.desc()).limit(1)
        )).scalars().first()

    if not fc or (scope is not None and fc.source not in scope):
        return JSONResponse({"error": "no data"}, status_code=404)

    try:
        geojson = json.loads(fc.geojson)
        lats, lons, grid = _extract_grid(geojson)
    except Exception:
        return JSONResponse({"error": "parse error"}, status_code=500)

    if len(lats) < 2 or len(lons) < 2:
        return JSONResponse({"error": "insufficient grid"}, status_code=500)

    # Apply lead-time scalar multiplier if requested and stats are available
    if lead != "total" and fc.lead_time_stats:
        try:
            lt = json.loads(fc.lead_time_stats)
            bucket = lt.get(lead)
            if bucket:
                total_mean = float(fc.precip_mean) if fc.precip_mean else 1.0
                bucket_mean = float(bucket.get("mean", total_mean))
                ratio = bucket_mean / total_mean if total_mean > 0 else 1.0
                grid = grid * ratio
        except Exception:
            pass

    try:
        png_bytes, lat_min, lat_max, lon_min, lon_max = _build_raster_png(lats, lons, grid, scale=3)
    except Exception as exc:
        import logging; logging.getLogger(__name__).error("raster render failed: %s", exc)
        return JSONResponse({"error": "render failed"}, status_code=500)

    headers = {
        "X-Lat-Min": str(lat_min),
        "X-Lat-Max": str(lat_max),
        "X-Lon-Min": str(lon_min),
        "X-Lon-Max": str(lon_max),
        "X-Forecast-Id": str(fc.id),
        "X-Source": fc.source or "",
        "X-Uploaded-At": fc.uploaded_at.isoformat(),
        "X-Precip-Max": str(fc.precip_max or 0),
        "Cache-Control": "no-cache",
        "Access-Control-Expose-Headers": "X-Lat-Min,X-Lat-Max,X-Lon-Min,X-Lon-Max,X-Forecast-Id,X-Source,X-Uploaded-At,X-Precip-Max",
    }
    return StreamingResponse(io.BytesIO(png_bytes), media_type="image/png", headers=headers)


@router.get("/layers/observed-raster")
async def layer_observed_raster(
    request: Request,
    obs_id: int = Query(..., description="ObservedRainfall record ID"),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    obs = (await db.execute(
        select(ObservedRainfall).where(ObservedRainfall.id == obs_id)
    )).scalars().first()

    if not obs or not obs.geojson:
        return JSONResponse({"error": "no data"}, status_code=404)

    try:
        geojson = json.loads(obs.geojson)
        lats, lons, grid = _extract_grid(geojson)
    except Exception:
        return JSONResponse({"error": "parse error"}, status_code=500)

    if len(lats) < 2 or len(lons) < 2:
        return JSONResponse({"error": "insufficient grid"}, status_code=500)

    try:
        png_bytes, lat_min, lat_max, lon_min, lon_max = _build_raster_png(lats, lons, grid, scale=3)
    except Exception as exc:
        import logging; logging.getLogger(__name__).error("observed raster render failed: %s", exc)
        return JSONResponse({"error": "render failed"}, status_code=500)

    headers = {
        "X-Lat-Min": str(lat_min), "X-Lat-Max": str(lat_max),
        "X-Lon-Min": str(lon_min), "X-Lon-Max": str(lon_max),
        "X-Precip-Max": str(obs.precip_max or 0),
        "Cache-Control": "no-cache",
        "Access-Control-Expose-Headers": "X-Lat-Min,X-Lat-Max,X-Lon-Min,X-Lon-Max,X-Precip-Max",
    }
    return StreamingResponse(io.BytesIO(png_bytes), media_type="image/png", headers=headers)


@router.get("/layers/glofas-raster")
async def layer_glofas_raster(
    request: Request,
    rec_id: int = Query(default=None, description="GlofasRecord ID; omit for latest"),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if rec_id is not None:
        rec = (await db.execute(
            select(GlofasRecord).where(GlofasRecord.id == rec_id)
        )).scalars().first()
    else:
        rec = (await db.execute(
            select(GlofasRecord)
            .where(GlofasRecord.geojson.isnot(None))
            .order_by(GlofasRecord.forecast_date.desc())
            .limit(1)
        )).scalars().first()

    if not rec or not rec.geojson:
        return JSONResponse({"error": "no data"}, status_code=404)

    try:
        geojson = json.loads(rec.geojson)
        lats, lons, grid = _extract_grid(geojson)
    except Exception:
        return JSONResponse({"error": "parse error"}, status_code=500)

    if len(lats) < 2 or len(lons) < 2:
        return JSONResponse({"error": "insufficient grid"}, status_code=500)

    try:
        png_bytes, lat_min, lat_max, lon_min, lon_max = _build_raster_png(
            lats, lons, grid, scale=2, rgba_fn=_discharge_rgba
        )
    except Exception as exc:
        import logging; logging.getLogger(__name__).error("glofas raster render failed: %s", exc)
        return JSONResponse({"error": "render failed"}, status_code=500)

    headers = {
        "X-Lat-Min": str(lat_min), "X-Lat-Max": str(lat_max),
        "X-Lon-Min": str(lon_min), "X-Lon-Max": str(lon_max),
        "X-Discharge-Max": str(rec.discharge_max or 0),
        "Cache-Control": "no-cache",
        "Access-Control-Expose-Headers": "X-Lat-Min,X-Lat-Max,X-Lon-Min,X-Lon-Max,X-Discharge-Max",
    }
    return StreamingResponse(io.BytesIO(png_bytes), media_type="image/png", headers=headers)


# ── Helpers shared by zonal + interpolation layers ───────────────────────────

def _pip(lat: float, lon: float, ring: list) -> bool:
    """Ray-casting point-in-polygon. ring is [[lon, lat], ...]."""
    n, inside, j = len(ring), False, len(ring) - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            if lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def _point_in_feature(lat: float, lon: float, geom: dict) -> bool:
    """Test a point against a GeoJSON Polygon or MultiPolygon geometry."""
    gtype = geom["type"]
    if gtype == "Polygon":
        rings = geom["coordinates"]
        if not _pip(lat, lon, rings[0]):
            return False
        for hole in rings[1:]:
            if _pip(lat, lon, hole):
                return False
        return True
    if gtype == "MultiPolygon":
        for poly in geom["coordinates"]:
            if not _pip(lat, lon, poly[0]):
                continue
            inside_hole = any(_pip(lat, lon, h) for h in poly[1:])
            if not inside_hole:
                return True
    return False


_COUNTRIES_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "countries.geojson")
_countries_cache: dict | None = None

def _load_countries() -> dict:
    global _countries_cache
    if _countries_cache is None:
        with open(_COUNTRIES_PATH) as f:
            _countries_cache = json.load(f)
    return _countries_cache


def _extract_grid(geojson: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Extract sorted unique lat/lon axes and 2D value grid from a FeatureCollection.
    Supports both Point features and Polygon cell features (uses centroid).
    """
    pts = []
    for feat in geojson.get("features", []):
        geom = feat.get("geometry", {})
        gtype = geom.get("type", "")
        coords = geom.get("coordinates", [])
        if gtype == "Point":
            lon, lat = coords[0], coords[1]
        elif gtype == "Polygon":
            # Use centroid of the bounding box of the outer ring
            ring = coords[0]
            lons_r = [c[0] for c in ring]
            lats_r = [c[1] for c in ring]
            lon = (min(lons_r) + max(lons_r)) / 2
            lat = (min(lats_r) + max(lats_r)) / 2
        else:
            continue
        val = feat["properties"].get("precip", feat["properties"].get("discharge", 0.0)) or 0.0
        pts.append((round(lat, 4), round(lon, 4), float(val)))

    lats_u = sorted({p[0] for p in pts})
    lons_u = sorted({p[1] for p in pts})
    lat_idx = {v: i for i, v in enumerate(lats_u)}
    lon_idx = {v: i for i, v in enumerate(lons_u)}

    grid = np.full((len(lats_u), len(lons_u)), np.nan)
    for lat, lon, val in pts:
        grid[lat_idx[lat], lon_idx[lon]] = val

    return np.array(lats_u), np.array(lons_u), grid


# ── Spatial interpolation layer ───────────────────────────────────────────────

@router.get("/layers/interpolated")
async def layer_interpolated(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    interp_stmt = (
        select(ForecastUpload)
        .where(ForecastUpload.geojson.isnot(None))
        .where(or_(ForecastUpload.variable == "tp", ForecastUpload.variable.is_(None)))
    )
    scope = allowed_sources(user)
    if scope is not None:
        interp_stmt = interp_stmt.where(ForecastUpload.source.in_(scope))
    fc = (await db.execute(
        interp_stmt.order_by(ForecastUpload.uploaded_at.desc()).limit(1)
    )).scalars().first()

    if not fc:
        return JSONResponse({"type": "FeatureCollection", "features": [], "meta": None})

    try:
        geojson = json.loads(fc.geojson)
        lats, lons, grid = _extract_grid(geojson)
    except Exception:
        return JSONResponse({"type": "FeatureCollection", "features": [], "meta": None})

    if len(lats) < 2 or len(lons) < 2:
        return JSONResponse(geojson)

    from scipy.interpolate import RegularGridInterpolator

    # Fill NaN holes row-wise before interpolating
    grid_filled = np.copy(grid)
    for i in range(grid_filled.shape[0]):
        row = grid_filled[i]
        valid = ~np.isnan(row)
        if valid.any():
            grid_filled[i] = np.interp(np.arange(len(row)), np.where(valid)[0], row[valid])

    # Upsample 2× using bilinear interpolation
    interp_fn = RegularGridInterpolator(
        (lats, lons), grid_filled, method="linear", bounds_error=False, fill_value=None
    )
    lat_fine = np.linspace(lats[0], lats[-1], len(lats) * 2)
    lon_fine = np.linspace(lons[0], lons[-1], len(lons) * 2)
    lon_grid, lat_grid = np.meshgrid(lon_fine, lat_fine)
    values_fine = interp_fn((lat_grid, lon_grid))

    features = []
    for i, lat in enumerate(lat_fine):
        for j, lon in enumerate(lon_fine):
            val = float(values_fine[i, j])
            if math.isnan(val):
                continue
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lon, 4), round(lat, 4)]},
                "properties": {"precip": round(val, 2)},
            })

    return JSONResponse({
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "forecast_id": fc.id,
            "source": fc.source,
            "uploaded_at": fc.uploaded_at.isoformat(),
            "precip_max": fc.precip_max,
            "interpolated": True,
        },
    })


# ── Zonal statistics layer ────────────────────────────────────────────────────

@router.get("/layers/zonal")
async def layer_zonal(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    zonal_stmt = (
        select(ForecastUpload)
        .where(ForecastUpload.geojson.isnot(None))
        .where(or_(ForecastUpload.variable == "tp", ForecastUpload.variable.is_(None)))
    )
    scope = allowed_sources(user)
    if scope is not None:
        zonal_stmt = zonal_stmt.where(ForecastUpload.source.in_(scope))
    fc = (await db.execute(
        zonal_stmt.order_by(ForecastUpload.uploaded_at.desc()).limit(1)
    )).scalars().first()

    if not fc:
        return JSONResponse({"type": "FeatureCollection", "features": [], "meta": None})

    try:
        geojson = json.loads(fc.geojson)
        countries = _load_countries()
    except Exception:
        return JSONResponse({"type": "FeatureCollection", "features": [], "meta": None})

    pts = []
    for feat in geojson.get("features", []):
        lon, lat = feat["geometry"]["coordinates"]
        val = feat["properties"].get("precip", 0.0) or 0.0
        pts.append((lat, lon, float(val)))

    if not pts:
        return JSONResponse({"type": "FeatureCollection", "features": [], "meta": None})

    global_max = max(v for _, _, v in pts) or 1.0

    features = []
    for country in countries.get("features", []):
        geom = country.get("geometry")
        props = country.get("properties", {})
        if not geom:
            continue

        vals = [v for lat, lon, v in pts if _point_in_feature(lat, lon, geom)]
        if not vals:
            continue

        mean_val = round(sum(vals) / len(vals), 2)
        max_val = round(max(vals), 2)
        intensity = round(min(mean_val / global_max, 1.0), 4)

        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "name": props.get("name") or props.get("NAME") or props.get("ADMIN") or "Unknown",
                "iso": props.get("iso_a2") or props.get("ISO_A2") or "",
                "precip_mean": mean_val,
                "precip_max": max_val,
                "n_cells": len(vals),
                "intensity": intensity,
            },
        })

    return JSONResponse({
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "forecast_id": fc.id,
            "source": fc.source,
            "uploaded_at": fc.uploaded_at.isoformat(),
            "precip_max": fc.precip_max,
        },
    })
