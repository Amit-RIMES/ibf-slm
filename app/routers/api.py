import asyncio
import json
import os
import tempfile
from datetime import date as date_type, datetime, timedelta, timezone
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_auth import require_api_key
from app.core.database import AsyncSessionLocal, get_db
from app.models.api_key import APIKey
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.sync import SyncConfig, SyncLog
from app.models.trigger import OPERATORS, VARIABLES, Trigger, TriggerActivation
from app.models.user import User

router = APIRouter(prefix="/api/v1")


# ── Request schemas ───────────────────────────────────────────────────────────

class ImpactCreate(BaseModel):
    event_name: str
    event_date: str
    hazard_type: str
    country: str
    region: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    affected_population: Optional[int] = None
    casualties: Optional[int] = None
    displaced: Optional[int] = None
    damage_usd: Optional[float] = None
    description: Optional[str] = None
    forecast_id: Optional[int] = None
    trigger_activation_id: Optional[int] = None


class ImpactUpdate(BaseModel):
    event_name: Optional[str] = None
    event_date: Optional[str] = None
    hazard_type: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    affected_population: Optional[int] = None
    casualties: Optional[int] = None
    displaced: Optional[int] = None
    damage_usd: Optional[float] = None
    description: Optional[str] = None
    forecast_id: Optional[int] = None
    trigger_activation_id: Optional[int] = None


class TriggerCreate(BaseModel):
    name: str
    hazard_type: str
    variable: str
    operator: str
    threshold: float
    is_active: bool = True
    scope_lat_min: Optional[float] = None
    scope_lat_max: Optional[float] = None
    scope_lon_min: Optional[float] = None
    scope_lon_max: Optional[float] = None
    condition_2_variable: Optional[str] = None
    condition_2_operator: Optional[str] = None
    condition_2_threshold: Optional[float] = None
    logic_op: Optional[str] = "and"


class TriggerUpdate(BaseModel):
    name: Optional[str] = None
    hazard_type: Optional[str] = None
    variable: Optional[str] = None
    operator: Optional[str] = None
    threshold: Optional[float] = None
    is_active: Optional[bool] = None
    scope_lat_min: Optional[float] = None
    scope_lat_max: Optional[float] = None
    scope_lon_min: Optional[float] = None
    scope_lon_max: Optional[float] = None
    condition_2_variable: Optional[str] = None
    condition_2_operator: Optional[str] = None
    condition_2_threshold: Optional[float] = None
    logic_op: Optional[str] = None


class ActivationAcknowledge(BaseModel):
    notes: Optional[str] = None


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


async def _scope_sources(key: APIKey, db: AsyncSession) -> list[str] | None:
    """Return allowed source keys for the API key's user, or None if no restriction."""
    user = await db.scalar(select(User).where(User.id == key.user_id))
    if not user or not user.country_scope:
        return None
    try:
        allowed = json.loads(user.country_scope)
    except Exception:
        return None
    return allowed if allowed else None


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


# ── SSE event bus ─────────────────────────────────────────────────────────────

_sse_subscribers: list[asyncio.Queue] = []


def broadcast_activation(activation_id: int, trigger_name: str, hazard_type: str) -> None:
    """Called from evaluate_triggers to push a real-time event to all SSE clients."""
    payload = json.dumps({"activation_id": activation_id, "trigger_name": trigger_name,
                          "hazard_type": hazard_type})
    dead = []
    for q in _sse_subscribers:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _sse_subscribers.remove(q)
        except ValueError:
            pass


async def _event_generator() -> AsyncGenerator[str, None]:
    q: asyncio.Queue = asyncio.Queue(maxsize=20)
    _sse_subscribers.append(q)
    try:
        yield "data: connected\n\n"
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25)
                yield f"data: {msg}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        try:
            _sse_subscribers.remove(q)
        except ValueError:
            pass


@router.get("/stream", summary="SSE stream for real-time activation events (requires API key)")
async def api_stream(_key: APIKey = Depends(require_api_key)):
    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Public endpoints ───────────────────────────────────────────────────────────

@router.get("/health", summary="System health (public)")
async def api_health():
    """Machine-readable health check for load balancers / k8s probes."""
    healthy = True
    checks: dict = {}
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(select(func.count()).select_from(ForecastUpload))
            fc_count = await db.scalar(select(func.count()).select_from(ForecastUpload))
            active_alerts = await db.scalar(
                select(func.count()).select_from(TriggerActivation)
                .where(TriggerActivation.status == "active")
            )
            cfg = await db.scalar(select(SyncConfig).where(SyncConfig.id == 1))
            checks["database"] = "ok"
            checks["forecast_count"] = fc_count or 0
            checks["active_alerts"] = active_alerts or 0
            checks["sync_enabled"] = bool(cfg and cfg.enabled) if cfg else False
            checks["last_sync_status"] = cfg.last_run_status if cfg else None
            checks["last_sync_at"] = cfg.last_run_at.isoformat() if cfg and cfg.last_run_at else None
    except Exception as exc:
        healthy = False
        checks["database"] = f"error: {exc}"

    return {
        "status": "healthy" if healthy else "unhealthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **checks,
    }


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
    allowed_sources = await _scope_sources(_key, db)
    if allowed_sources is not None:
        filters.append(ForecastUpload.source.in_(allowed_sources))
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


@router.post("/forecasts/upload", summary="Upload a NetCDF forecast file", status_code=201)
async def api_upload_forecast(
    file: UploadFile = File(..., description="NetCDF (.nc) forecast file"),
    source: str = Query(..., description="Source key, e.g. country_bd or regional_rimes"),
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    from app.core.anomaly import compute_anomaly
    from app.routers.forecasts import _process_netcdf
    from app.routers.triggers import evaluate_triggers

    if not file.filename or not file.filename.endswith(".nc"):
        raise HTTPException(status_code=400, detail="File must be a NetCDF (.nc) file")

    # Reject if the user's scope doesn't cover this source
    allowed_sources = await _scope_sources(_key, db)
    if allowed_sources is not None and source not in allowed_sources:
        raise HTTPException(status_code=403, detail=f"Your API key is not scoped to source '{source}'")

    # Check for duplicate filename on same source
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"api_{source}_{date_str}_{file.filename}"
    existing = await db.scalar(select(ForecastUpload).where(ForecastUpload.filename == filename))
    if existing:
        raise HTTPException(status_code=409, detail="A forecast with this filename already exists today")

    # Write to a temp file and process
    suffix = os.path.splitext(file.filename)[-1] or ".nc"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        meta = await __import__("asyncio").get_event_loop().run_in_executor(None, _process_netcdf, tmp_path)
    except Exception as exc:
        os.unlink(tmp_path)
        raise HTTPException(status_code=422, detail=f"Failed to process NetCDF file: {exc}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    fc = ForecastUpload(
        filename=filename,
        source=source,
        lat_min=meta["lat_min"], lat_max=meta["lat_max"],
        lon_min=meta["lon_min"], lon_max=meta["lon_max"],
        time_start=meta["time_start"], time_end=meta["time_end"],
        time_steps=meta["time_steps"],
        precip_min=meta["precip_min"], precip_max=meta["precip_max"],
        precip_mean=meta["precip_mean"],
        geojson=meta["geojson"],
    )
    db.add(fc)
    await db.flush()

    # Anomaly detection
    anomaly_score, is_anomaly = await compute_anomaly(fc, db)
    fc.anomaly_score = anomaly_score
    fc.is_anomaly = is_anomaly

    await db.commit()
    await db.refresh(fc)

    # Evaluate triggers
    import asyncio
    asyncio.create_task(evaluate_triggers(fc, db))

    return _forecast_dict(fc)


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
    allowed_sources = await _scope_sources(_key, db)
    if allowed_sources is not None:
        filters.append(
            TriggerActivation.forecast_id.in_(
                select(ForecastUpload.id).where(ForecastUpload.source.in_(allowed_sources))
            )
        )
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

    stmt = select(ImpactRecord)  # type: ignore[assignment]
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


@router.post("/impacts", summary="Create an impact record", status_code=201)
async def api_create_impact(
    body: ImpactCreate,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    try:
        ev_date = date_type.fromisoformat(body.event_date)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid event_date: '{body.event_date}'")

    if body.forecast_id:
        exists = await db.scalar(select(ForecastUpload).where(ForecastUpload.id == body.forecast_id))
        if not exists:
            raise HTTPException(status_code=400, detail=f"forecast_id {body.forecast_id} not found")
    if body.trigger_activation_id:
        exists = await db.scalar(select(TriggerActivation).where(TriggerActivation.id == body.trigger_activation_id))
        if not exists:
            raise HTTPException(status_code=400, detail=f"trigger_activation_id {body.trigger_activation_id} not found")

    imp = ImpactRecord(
        event_name=body.event_name, event_date=ev_date, hazard_type=body.hazard_type,
        country=body.country, region=body.region, lat=body.lat, lon=body.lon,
        affected_population=body.affected_population, casualties=body.casualties,
        displaced=body.displaced, damage_usd=body.damage_usd,
        description=body.description, forecast_id=body.forecast_id,
        trigger_activation_id=body.trigger_activation_id,
    )
    db.add(imp)
    await db.commit()
    await db.refresh(imp)
    return _impact_dict(imp)


@router.patch("/impacts/{impact_id}", summary="Update an impact record")
async def api_update_impact(
    impact_id: int,
    body: ImpactUpdate,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    result = await db.execute(select(ImpactRecord).where(ImpactRecord.id == impact_id))
    imp = result.scalar_one_or_none()
    if not imp:
        raise HTTPException(status_code=404, detail="Impact record not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "event_date" and value is not None:
            try:
                value = date_type.fromisoformat(value)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid event_date: '{value}'")
        setattr(imp, field, value)

    await db.commit()
    await db.refresh(imp)
    return _impact_dict(imp)


@router.delete("/impacts/{impact_id}", summary="Delete an impact record", status_code=204)
async def api_delete_impact(
    impact_id: int,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    result = await db.execute(select(ImpactRecord).where(ImpactRecord.id == impact_id))
    imp = result.scalar_one_or_none()
    if not imp:
        raise HTTPException(status_code=404, detail="Impact record not found")
    await db.delete(imp)
    await db.commit()


# ── Trigger write endpoints ───────────────────────────────────────────────────

@router.post("/triggers", summary="Create a trigger", status_code=201)
async def api_create_trigger(
    body: TriggerCreate,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    if body.variable not in VARIABLES:
        raise HTTPException(status_code=400, detail=f"variable must be one of {VARIABLES}")
    if body.operator not in OPERATORS:
        raise HTTPException(status_code=400, detail=f"operator must be one of {OPERATORS}")
    if body.condition_2_variable and body.condition_2_variable not in VARIABLES:
        raise HTTPException(status_code=400, detail=f"condition_2_variable must be one of {VARIABLES}")
    if body.condition_2_operator and body.condition_2_operator not in OPERATORS:
        raise HTTPException(status_code=400, detail=f"condition_2_operator must be one of {OPERATORS}")

    trigger = Trigger(
        name=body.name, hazard_type=body.hazard_type, variable=body.variable,
        operator=body.operator, threshold=body.threshold, is_active=body.is_active,
        scope_lat_min=body.scope_lat_min, scope_lat_max=body.scope_lat_max,
        scope_lon_min=body.scope_lon_min, scope_lon_max=body.scope_lon_max,
        condition_2_variable=body.condition_2_variable,
        condition_2_operator=body.condition_2_operator,
        condition_2_threshold=body.condition_2_threshold,
        logic_op=body.logic_op or "and",
    )
    db.add(trigger)
    await db.commit()
    await db.refresh(trigger)
    return _trigger_dict(trigger)


@router.patch("/triggers/{trigger_id}", summary="Update a trigger")
async def api_update_trigger(
    trigger_id: int,
    body: TriggerUpdate,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = result.scalar_one_or_none()
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")

    updates = body.model_dump(exclude_unset=True)
    if "variable" in updates and updates["variable"] not in VARIABLES:
        raise HTTPException(status_code=400, detail=f"variable must be one of {VARIABLES}")
    if "operator" in updates and updates["operator"] not in OPERATORS:
        raise HTTPException(status_code=400, detail=f"operator must be one of {OPERATORS}")

    for field, value in updates.items():
        setattr(trigger, field, value)

    await db.commit()
    await db.refresh(trigger)
    return _trigger_dict(trigger)


@router.delete("/triggers/{trigger_id}", summary="Deactivate a trigger", status_code=200)
async def api_delete_trigger(
    trigger_id: int,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    result = await db.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = result.scalar_one_or_none()
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")
    trigger.is_active = False
    await db.commit()
    return {"id": trigger_id, "is_active": False}


# ── Activation write endpoints ────────────────────────────────────────────────

@router.post("/activations/{activation_id}/acknowledge", summary="Acknowledge an activation")
async def api_acknowledge_activation(
    activation_id: int,
    body: ActivationAcknowledge,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_api_key),
):
    result = await db.execute(
        select(TriggerActivation).where(TriggerActivation.id == activation_id)
    )
    activation = result.scalar_one_or_none()
    if not activation:
        raise HTTPException(status_code=404, detail="Activation not found")
    if activation.status == "acknowledged":
        raise HTTPException(status_code=400, detail="Activation is already acknowledged")

    activation.status = "acknowledged"
    activation.acknowledged_at = datetime.now(timezone.utc)
    activation.notes = body.notes
    await db.commit()
    return _activation_dict(activation)
