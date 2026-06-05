from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_auth import require_api_key
from app.core.database import get_db
from app.models.api_key import APIKey
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.trigger import Trigger, TriggerActivation

router = APIRouter(prefix="/api/v1")


def _forecast_dict(fc: ForecastUpload) -> dict:
    return {
        "id": fc.id,
        "filename": fc.filename,
        "uploaded_at": fc.uploaded_at.isoformat(),
        "lat_min": fc.lat_min, "lat_max": fc.lat_max,
        "lon_min": fc.lon_min, "lon_max": fc.lon_max,
        "time_start": fc.time_start,
        "time_end": fc.time_end,
        "time_steps": fc.time_steps,
        "precip_min": fc.precip_min,
        "precip_max": fc.precip_max,
        "precip_mean": fc.precip_mean,
    }


def _impact_dict(imp: ImpactRecord) -> dict:
    return {
        "id": imp.id,
        "event_name": imp.event_name,
        "event_date": str(imp.event_date),
        "hazard_type": imp.hazard_type,
        "country": imp.country,
        "region": imp.region,
        "lat": imp.lat,
        "lon": imp.lon,
        "affected_population": imp.affected_population,
        "casualties": imp.casualties,
        "displaced": imp.displaced,
        "damage_usd": imp.damage_usd,
        "description": imp.description,
        "forecast_id": imp.forecast_id,
        "created_at": imp.created_at.isoformat(),
    }


def _trigger_dict(t: Trigger) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "hazard_type": t.hazard_type,
        "variable": t.variable,
        "operator": t.operator,
        "threshold": t.threshold,
        "is_active": t.is_active,
        "created_at": t.created_at.isoformat(),
    }


def _activation_dict(a: TriggerActivation) -> dict:
    return {
        "id": a.id,
        "trigger_id": a.trigger_id,
        "trigger_name": a.trigger.name if a.trigger else None,
        "hazard_type": a.trigger.hazard_type if a.trigger else None,
        "variable": a.trigger.variable if a.trigger else None,
        "operator": a.trigger.operator if a.trigger else None,
        "threshold": a.trigger.threshold if a.trigger else None,
        "value": a.value,
        "status": a.status,
        "triggered_at": a.triggered_at.isoformat(),
        "acknowledged_at": a.acknowledged_at.isoformat() if a.acknowledged_at else None,
        "forecast_id": a.forecast_id,
        "forecast_filename": a.forecast.filename if a.forecast else None,
    }


# ── Public endpoint (no auth) ──────────────────────────────────────────────

@router.get("/status")
async def api_status(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(TriggerActivation)
        .where(TriggerActivation.status == "active")
        .order_by(desc(TriggerActivation.triggered_at))
    )
    activations = result.scalars().all()
    alerts = [
        {
            "hazard_type": a.trigger.hazard_type if a.trigger else None,
            "triggered_at": a.triggered_at.isoformat(),
            "region": {
                "lat_min": a.forecast.lat_min, "lat_max": a.forecast.lat_max,
                "lon_min": a.forecast.lon_min, "lon_max": a.forecast.lon_max,
            } if a.forecast else None,
        }
        for a in activations if a.forecast
    ]
    return {"alert_count": len(alerts), "updated_at": datetime.now(timezone.utc).isoformat(), "alerts": alerts}


# ── Authenticated endpoints ────────────────────────────────────────────────

@router.get("/forecasts")
async def api_forecasts(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    total = await db.scalar(select(__import__("sqlalchemy").func.count()).select_from(ForecastUpload))
    result = await db.execute(
        select(ForecastUpload)
        .order_by(desc(ForecastUpload.uploaded_at))
        .offset((page - 1) * limit).limit(limit)
    )
    return {"total": total, "page": page, "limit": limit, "data": [_forecast_dict(f) for f in result.scalars()]}


@router.get("/forecasts/{forecast_id}")
async def api_forecast(
    forecast_id: int,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    from fastapi import HTTPException
    result = await db.execute(select(ForecastUpload).where(ForecastUpload.id == forecast_id))
    fc = result.scalar_one_or_none()
    if not fc:
        raise HTTPException(status_code=404, detail="Forecast not found")
    return _forecast_dict(fc)


@router.get("/impacts")
async def api_impacts(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    hazard_type: str = Query(None),
    country: str = Query(None),
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    from sqlalchemy import func, and_
    stmt = select(ImpactRecord)
    filters = []
    if hazard_type:
        filters.append(ImpactRecord.hazard_type == hazard_type)
    if country:
        filters.append(ImpactRecord.country.ilike(f"%{country}%"))
    if filters:
        stmt = stmt.where(and_(*filters))

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = await db.scalar(count_stmt)
    result = await db.execute(stmt.order_by(desc(ImpactRecord.event_date)).offset((page - 1) * limit).limit(limit))
    return {"total": total, "page": page, "limit": limit, "data": [_impact_dict(i) for i in result.scalars()]}


@router.get("/impacts/{impact_id}")
async def api_impact(
    impact_id: int,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    from fastapi import HTTPException
    result = await db.execute(select(ImpactRecord).where(ImpactRecord.id == impact_id))
    imp = result.scalar_one_or_none()
    if not imp:
        raise HTTPException(status_code=404, detail="Impact record not found")
    return _impact_dict(imp)


@router.get("/triggers")
async def api_triggers(
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    result = await db.execute(select(Trigger).order_by(Trigger.id))
    return {"data": [_trigger_dict(t) for t in result.scalars()]}


@router.get("/activations")
async def api_activations(
    status: str = Query("active"),
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    stmt = select(TriggerActivation).order_by(desc(TriggerActivation.triggered_at))
    if status != "all":
        stmt = stmt.where(TriggerActivation.status == status)
    result = await db.execute(stmt)
    return {"data": [_activation_dict(a) for a in result.scalars()]}
