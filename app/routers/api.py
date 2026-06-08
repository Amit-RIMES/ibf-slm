from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_auth import require_api_key
from app.core.database import get_db
from app.models.api_key import APIKey
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.trigger import Trigger, TriggerActivation

router = APIRouter(prefix="/api/v1")


# ── Serialisers ───────────────────────────────────────────────────────────────

def _forecast_dict(fc: ForecastUpload) -> dict:
    return {
        "id": fc.id,
        "filename": fc.filename,
        "source": fc.source,
        "uploaded_at": fc.uploaded_at.isoformat(),
        "lat_min": fc.lat_min, "lat_max": fc.lat_max,
        "lon_min": fc.lon_min, "lon_max": fc.lon_max,
        "time_start": fc.time_start,
        "time_end": fc.time_end,
        "time_steps": fc.time_steps,
        "precip_min": fc.precip_min,
        "precip_max": fc.precip_max,
        "precip_mean": fc.precip_mean,
        "is_anomaly": fc.is_anomaly,
        "anomaly_score": fc.anomaly_score,
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
        "scope_lat_min": t.scope_lat_min,
        "scope_lat_max": t.scope_lat_max,
        "scope_lon_min": t.scope_lon_min,
        "scope_lon_max": t.scope_lon_max,
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
        "notes": a.notes,
        "triggered_at": a.triggered_at.isoformat(),
        "acknowledged_at": a.acknowledged_at.isoformat() if a.acknowledged_at else None,
        "forecast_id": a.forecast_id,
        "forecast_filename": a.forecast.filename if a.forecast else None,
        "forecast_source": a.forecast.source if a.forecast else None,
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
        "trigger_activation_id": imp.trigger_activation_id,
        "created_at": imp.created_at.isoformat(),
    }


def _paginate(total: int, page: int, limit: int) -> dict:
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "pages": max(1, -(-total // limit)),
    }


def _parse_date(value: str, end_of_day: bool = False):
    """Parse ISO date string to UTC datetime, or raise HTTPException 400."""
    try:
        dt = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        if end_of_day:
            dt = (dt + timedelta(days=1))
        return dt
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date format: '{value}'. Use YYYY-MM-DD.")


# ── Public endpoint ────────────────────────────────────────────────────────────

@router.get("/status", summary="Active alert status (public)")
async def api_status(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(TriggerActivation)
        .where(TriggerActivation.status == "active")
        .order_by(desc(TriggerActivation.triggered_at))
    )
    activations = result.scalars().all()
    alerts = [
        {
            "trigger_id": a.trigger_id,
            "trigger_name": a.trigger.name if a.trigger else None,
            "hazard_type": a.trigger.hazard_type if a.trigger else None,
            "value": a.value,
            "triggered_at": a.triggered_at.isoformat(),
            "forecast_filename": a.forecast.filename if a.forecast else None,
            "region": {
                "lat_min": a.forecast.lat_min, "lat_max": a.forecast.lat_max,
                "lon_min": a.forecast.lon_min, "lon_max": a.forecast.lon_max,
            } if a.forecast else None,
        }
        for a in activations if a.forecast
    ]
    return {
        "alert_count": len(alerts),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "alerts": alerts,
    }


# ── Forecasts ─────────────────────────────────────────────────────────────────

@router.get("/forecasts", summary="List forecasts")
async def api_forecasts(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    source: str = Query(None, description="Filter by source key"),
    date_from: str = Query(None, description="Uploaded on or after (YYYY-MM-DD)"),
    date_to: str = Query(None, description="Uploaded on or before (YYYY-MM-DD)"),
    anomaly_only: bool = Query(False, description="Return only anomalous forecasts"),
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    filters = []
    if source:
        filters.append(ForecastUpload.source == source)
    if date_from:
        filters.append(ForecastUpload.uploaded_at >= _parse_date(date_from))
    if date_to:
        filters.append(ForecastUpload.uploaded_at < _parse_date(date_to, end_of_day=True))
    if anomaly_only:
        filters.append(ForecastUpload.is_anomaly == True)  # noqa: E712

    stmt = select(ForecastUpload)
    if filters:
        stmt = stmt.where(and_(*filters))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    result = await db.execute(
        stmt.order_by(desc(ForecastUpload.uploaded_at))
        .offset((page - 1) * limit).limit(limit)
    )
    return {**_paginate(total, page, limit), "data": [_forecast_dict(f) for f in result.scalars()]}


@router.get("/forecasts/{forecast_id}", summary="Get a single forecast")
async def api_forecast(
    forecast_id: int,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    result = await db.execute(select(ForecastUpload).where(ForecastUpload.id == forecast_id))
    fc = result.scalar_one_or_none()
    if not fc:
        raise HTTPException(status_code=404, detail="Forecast not found")
    return _forecast_dict(fc)


# ── Triggers ──────────────────────────────────────────────────────────────────

@router.get("/triggers", summary="List triggers")
async def api_triggers(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    is_active: bool = Query(None, description="Filter by active status"),
    hazard_type: str = Query(None, description="Filter by hazard type"),
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    filters = []
    if is_active is not None:
        filters.append(Trigger.is_active == is_active)
    if hazard_type:
        filters.append(Trigger.hazard_type == hazard_type)

    stmt = select(Trigger)
    if filters:
        stmt = stmt.where(and_(*filters))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    result = await db.execute(
        stmt.order_by(Trigger.id).offset((page - 1) * limit).limit(limit)
    )
    return {**_paginate(total, page, limit), "data": [_trigger_dict(t) for t in result.scalars()]}


@router.get("/triggers/{trigger_id}", summary="Get a single trigger")
async def api_trigger(
    trigger_id: int,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Trigger not found")
    return _trigger_dict(t)


# ── Activations ───────────────────────────────────────────────────────────────

@router.get("/activations", summary="List trigger activations")
async def api_activations(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: str = Query(None, description="active | acknowledged | all (default: all)"),
    trigger_id: int = Query(None, description="Filter by trigger ID"),
    hazard_type: str = Query(None, description="Filter by hazard type"),
    date_from: str = Query(None, description="Triggered on or after (YYYY-MM-DD)"),
    date_to: str = Query(None, description="Triggered on or before (YYYY-MM-DD)"),
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    filters = []
    if status and status != "all":
        if status not in ("active", "acknowledged"):
            raise HTTPException(status_code=400, detail="status must be 'active', 'acknowledged', or 'all'")
        filters.append(TriggerActivation.status == status)
    if trigger_id:
        filters.append(TriggerActivation.trigger_id == trigger_id)
    if hazard_type:
        from app.models.trigger import Trigger as _Trigger
        filters.append(
            TriggerActivation.trigger_id.in_(
                select(_Trigger.id).where(_Trigger.hazard_type == hazard_type)
            )
        )
    if date_from:
        filters.append(TriggerActivation.triggered_at >= _parse_date(date_from))
    if date_to:
        filters.append(TriggerActivation.triggered_at < _parse_date(date_to, end_of_day=True))

    stmt = select(TriggerActivation)
    if filters:
        stmt = stmt.where(and_(*filters))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    result = await db.execute(
        stmt.order_by(desc(TriggerActivation.triggered_at))
        .offset((page - 1) * limit).limit(limit)
    )
    return {**_paginate(total, page, limit), "data": [_activation_dict(a) for a in result.scalars()]}


@router.get("/activations/{activation_id}", summary="Get a single activation")
async def api_activation(
    activation_id: int,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    result = await db.execute(
        select(TriggerActivation).where(TriggerActivation.id == activation_id)
    )
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Activation not found")
    return _activation_dict(a)


# ── Impacts ───────────────────────────────────────────────────────────────────

@router.get("/impacts", summary="List impact records")
async def api_impacts(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    q: str = Query(None, description="Search event name"),
    hazard_type: str = Query(None, description="Filter by hazard type"),
    country: str = Query(None, description="Filter by country (partial match)"),
    date_from: str = Query(None, description="Event date on or after (YYYY-MM-DD)"),
    date_to: str = Query(None, description="Event date on or before (YYYY-MM-DD)"),
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    from datetime import date as date_type
    filters = []
    if q:
        filters.append(ImpactRecord.event_name.ilike(f"%{q}%"))
    if hazard_type:
        filters.append(ImpactRecord.hazard_type == hazard_type)
    if country:
        filters.append(ImpactRecord.country.ilike(f"%{country}%"))
    if date_from:
        try:
            filters.append(ImpactRecord.event_date >= date_type.fromisoformat(date_from))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date_from: '{date_from}'")
    if date_to:
        try:
            filters.append(ImpactRecord.event_date <= date_type.fromisoformat(date_to))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date_to: '{date_to}'")

    stmt = select(ImpactRecord)
    if filters:
        stmt = stmt.where(and_(*filters))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    result = await db.execute(
        stmt.order_by(desc(ImpactRecord.event_date))
        .offset((page - 1) * limit).limit(limit)
    )
    return {**_paginate(total, page, limit), "data": [_impact_dict(i) for i in result.scalars()]}


@router.get("/impacts/{impact_id}", summary="Get a single impact record")
async def api_impact(
    impact_id: int,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    result = await db.execute(select(ImpactRecord).where(ImpactRecord.id == impact_id))
    imp = result.scalar_one_or_none()
    if not imp:
        raise HTTPException(status_code=404, detail="Impact record not found")
    return _impact_dict(imp)
