import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.impact import ImpactRecord
from app.models.risk_history import RiskScoreRecord
from app.models.spi import SPIRecord
from app.models.trigger import Trigger, TriggerActivation

router = APIRouter(prefix="/risk")
templates = Jinja2Templates(directory="app/templates")

_LEVEL_COLOR = {
    "Extreme": "#dc2626",
    "High": "#f97316",
    "Moderate": "#f59e0b",
    "Low": "#22c55e",
}


@router.get("", response_class=HTMLResponse)
async def risk_overview(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    # Sources that have SPI data (plus any with risk history)
    spi_sources_r = await db.execute(select(SPIRecord.source).distinct())
    spi_sources = {r[0] for r in spi_sources_r.all()}

    hist_sources_r = await db.execute(select(RiskScoreRecord.source).distinct())
    hist_sources = {r[0] for r in hist_sources_r.all()}

    all_sources = sorted(spi_sources | hist_sources) or ["CHIRPS"]

    # Latest record per source
    latest_r = await db.execute(
        select(RiskScoreRecord).order_by(RiskScoreRecord.scored_at.desc()).limit(200)
    )
    latest_by_source: dict[str, RiskScoreRecord] = {}
    for rec in latest_r.scalars().all():
        if rec.source not in latest_by_source:
            latest_by_source[rec.source] = rec

    # 7-day sparkline per source
    spark_by_source: dict[str, list] = {s: [] for s in all_sources}
    spark_r = await db.execute(
        select(RiskScoreRecord).order_by(
            RiskScoreRecord.source, RiskScoreRecord.scored_at.desc()
        ).limit(500)
    )
    for rec in spark_r.scalars().all():
        if rec.source in spark_by_source and len(spark_by_source[rec.source]) < 7:
            spark_by_source[rec.source].append(rec)
    # Reverse each to chronological order
    for s in spark_by_source:
        spark_by_source[s] = list(reversed(spark_by_source[s]))

    cards = []
    for source in all_sources:
        latest = latest_by_source.get(source)
        if latest:
            color = _LEVEL_COLOR.get(latest.level, "#22c55e")
            ts = latest.scored_at if latest.scored_at.tzinfo else latest.scored_at.replace(tzinfo=timezone.utc)
            card = {
                "source": source,
                "total": latest.total,
                "level": latest.level,
                "level_color": color,
                "spi_pts": latest.spi_pts,
                "seasonal_pts": latest.seasonal_pts,
                "trigger_pts": latest.trigger_pts,
                "worst_spi": latest.worst_spi,
                "last_updated": ts.strftime("%b %d %H:%M UTC"),
                "has_data": True,
                "sparkline_json": json.dumps([
                    {
                        "label": (r.scored_at if r.scored_at.tzinfo else r.scored_at.replace(tzinfo=timezone.utc)).strftime("%b %d"),
                        "total": r.total,
                    }
                    for r in spark_by_source.get(source, [])
                ]),
            }
        else:
            card = {
                "source": source,
                "total": 0,
                "level": "Low",
                "level_color": "#22c55e",
                "spi_pts": 0,
                "seasonal_pts": 0,
                "trigger_pts": 0,
                "worst_spi": None,
                "last_updated": None,
                "has_data": False,
                "sparkline_json": "[]",
            }
        cards.append(card)

    # Sort by risk score descending
    cards.sort(key=lambda c: c["total"], reverse=True)

    return templates.TemplateResponse(
        request,
        "risk_overview.html",
        {"user": user, "cards": cards},
    )


@router.get("/map", response_class=HTMLResponse)
async def risk_map(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    # All triggers with their scope info
    triggers_r = await db.execute(select(Trigger).order_by(Trigger.hazard_type, Trigger.name))
    all_triggers = triggers_r.scalars().all()

    # Active activations
    active_ids_r = await db.execute(
        select(TriggerActivation.trigger_id).where(TriggerActivation.status == "active").distinct()
    )
    active_trigger_ids = {r[0] for r in active_ids_r.all()}

    # Recent impacts grouped by country (last 365 days worth, no date filter for simplicity)
    country_rows = (await db.execute(
        select(ImpactRecord.country, func.count().label("cnt"))
        .group_by(ImpactRecord.country)
        .order_by(func.count().desc())
    )).all()
    country_stats = [{"country": r.country, "count": r.cnt} for r in country_rows if r.country]

    # Build GeoJSON features for map
    features = []
    HAZARD_COLORS = {
        "flood": "#3b82f6", "storm": "#8b5cf6", "drought": "#f59e0b",
        "landslide": "#10b981", "heatwave": "#ef4444", "cyclone": "#0ea5e9", "other": "#6b7280",
    }
    for t in all_triggers:
        is_active_alert = t.id in active_trigger_ids
        color = HAZARD_COLORS.get(t.hazard_type or "other", "#6b7280")
        props = {
            "id": t.id,
            "name": t.name,
            "hazard_type": t.hazard_type or "other",
            "is_active": t.is_active,
            "is_alert": is_active_alert,
            "color": "#dc2626" if is_active_alert else color,
        }
        geom = None
        if t.scope_polygon:
            try:
                ring = json.loads(t.scope_polygon)
                geom = {"type": "Polygon", "coordinates": [ring]}
            except Exception:
                pass
        elif t.scope_lat_min is not None:
            mn_la, mx_la = t.scope_lat_min, t.scope_lat_max
            mn_lo, mx_lo = t.scope_lon_min, t.scope_lon_max
            geom = {"type": "Polygon", "coordinates": [
                [[mn_lo, mn_la], [mx_lo, mn_la], [mx_lo, mx_la], [mn_lo, mx_la], [mn_lo, mn_la]]
            ]}
        features.append({"type": "Feature", "geometry": geom, "properties": props})

    geojson = json.dumps({"type": "FeatureCollection", "features": features})

    # Hazard breakdown
    hazard_rows = (await db.execute(
        select(Trigger.hazard_type, func.count().label("cnt"))
        .group_by(Trigger.hazard_type)
        .order_by(func.count().desc())
    )).all()
    hazard_stats = [
        {"hazard": r.hazard_type or "other", "count": r.cnt,
         "color": HAZARD_COLORS.get(r.hazard_type or "other", "#6b7280")}
        for r in hazard_rows
    ]

    return templates.TemplateResponse(
        request,
        "risk_map.html",
        {
            "user": user,
            "geojson": geojson,
            "country_stats": country_stats,
            "hazard_stats": hazard_stats,
            "total_triggers": len(all_triggers),
            "active_alert_count": len(active_trigger_ids),
        },
    )
