"""
ECMWF Open Data admin configuration and manual trigger.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.ecmwf_config import EcmwfConfig
from app.models.forecast import ForecastUpload
from app.models.job_run import JobRun

router = APIRouter(prefix="/ecmwf")
templates = Jinja2Templates(directory="app/templates")

_FORBIDDEN = HTMLResponse(
    "<h1 style='font-family:system-ui;margin:3rem auto;max-width:400px'>403 — Admin access required</h1>",
    status_code=403,
)


async def _get_or_create_config(db: AsyncSession) -> EcmwfConfig:
    cfg = await db.scalar(select(EcmwfConfig).where(EcmwfConfig.id == 1))
    if not cfg:
        cfg = EcmwfConfig(
            id=1, enabled=False, use_ensemble=False,
            run_time=0, sync_hour=10, sync_minute=0,
            lat_min=0.0, lat_max=35.0, lon_min=60.0, lon_max=155.0,
        )
        db.add(cfg)
        await db.commit()
        await db.refresh(cfg)
    return cfg


@router.get("", response_class=HTMLResponse)
async def ecmwf_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    cfg = await _get_or_create_config(db)

    # Recent ECMWF forecast records
    recent_r = await db.execute(
        select(ForecastUpload)
        .where(ForecastUpload.source.in_(["ECMWF-IFS-HRES", "ECMWF-IFS-ENS"]))
        .order_by(ForecastUpload.uploaded_at.desc())
        .limit(10)
    )
    recent = recent_r.scalars().all()

    # Job run history for ecmwf_sync
    runs_r = await db.execute(
        select(JobRun)
        .where(JobRun.job_name == "ecmwf_sync")
        .order_by(JobRun.started_at.desc())
        .limit(15)
    )
    runs = runs_r.scalars().all()

    return templates.TemplateResponse(
        request, "ecmwf_config.html",
        {"user": user, "cfg": cfg, "recent": recent, "runs": runs},
    )


@router.post("/config", response_class=HTMLResponse)
async def update_config(
    request: Request,
    enabled: str = Form(default="off"),
    use_ensemble: str = Form(default="off"),
    run_time: int = Form(0),
    sync_hour: int = Form(10),
    sync_minute: int = Form(0),
    lat_min: float = Form(0.0),
    lat_max: float = Form(35.0),
    lon_min: float = Form(60.0),
    lon_max: float = Form(155.0),
    db: AsyncSession = Depends(get_db),
):
    import json as _json
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    # Extract multi-value 'parameters' checkboxes from the already-cached form data
    raw_form = await request.form()
    selected_params = raw_form.getlist("parameters")
    valid_params = {"tp", "2t", "wind10", "msl"}
    clean_params = [p for p in selected_params if p in valid_params] or ["tp"]

    cfg = await _get_or_create_config(db)
    cfg.enabled = enabled == "on"
    cfg.use_ensemble = use_ensemble == "on"
    cfg.run_time = run_time if run_time in (0, 6, 12, 18) else 0
    cfg.sync_hour = max(0, min(23, sync_hour))
    cfg.sync_minute = max(0, min(59, sync_minute))
    cfg.lat_min = lat_min
    cfg.lat_max = lat_max
    cfg.lon_min = lon_min
    cfg.lon_max = lon_max
    cfg.parameters = _json.dumps(clean_params)
    await db.commit()

    try:
        from app.scheduler import apply_ecmwf_schedule
        await apply_ecmwf_schedule()
    except Exception:
        pass  # scheduler update is best-effort; config is already persisted

    return RedirectResponse("/ecmwf", status_code=303)


async def _do_fetch_now(cfg_snapshot: dict):
    """Background task: fetch latest ECMWF forecast for all configured parameters."""
    import json as _json
    from app.core.database import AsyncSessionLocal
    from app.core.anomaly import compute_anomaly
    from app.core.ecmwf_opendata import fetch_ecmwf_forecast
    from app.routers.triggers import evaluate_triggers

    started_at = datetime.now(timezone.utc)
    status = "error"
    detail = ""
    parameters = _json.loads(cfg_snapshot.get("parameters") or '["tp"]')

    imported_count = 0
    errors: list[str] = []

    try:
        for variable in parameters:
            data = await fetch_ecmwf_forecast(
                lat_min=cfg_snapshot["lat_min"],
                lat_max=cfg_snapshot["lat_max"],
                lon_min=cfg_snapshot["lon_min"],
                lon_max=cfg_snapshot["lon_max"],
                run_time=cfg_snapshot["run_time"],
                use_ensemble=cfg_snapshot["use_ensemble"],
                variable=variable,
            )
            if data is None:
                errors.append(f"{variable}: fetch returned None")
                continue
            async with AsyncSessionLocal() as db:
                existing = await db.scalar(
                    select(ForecastUpload).where(ForecastUpload.filename == data["filename"])
                )
                if existing:
                    continue
                lead_time_stats = data.pop("lead_time_stats", None)
                forecast = ForecastUpload(lead_time_stats=lead_time_stats, **data)
                db.add(forecast)
                await db.commit()
                await db.refresh(forecast)
                await compute_anomaly(forecast, db)
                await evaluate_triggers(forecast, db)
                imported_count += 1

        if errors:
            detail = "; ".join(errors)
            status = "error" if imported_count == 0 else "partial"
        else:
            status = "ok"
            detail = f"Imported {imported_count} forecast(s) for params: {parameters}"

        async with AsyncSessionLocal() as db:
            cfg = await db.scalar(select(EcmwfConfig).where(EcmwfConfig.id == 1))
            if cfg:
                cfg.last_run_at = datetime.now(timezone.utc)
                cfg.last_run_status = status
                cfg.last_run_detail = detail[:512]
                await db.commit()
    except Exception as exc:
        detail = str(exc)[:512]

    from app.models.job_run import JobRun
    async with AsyncSessionLocal() as db:
        db.add(JobRun(
            job_name="ecmwf_sync",
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            status=status,
            detail=detail[:500],
        ))
        await db.commit()


@router.post("/fetch-now", response_class=HTMLResponse)
async def fetch_now(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    cfg = await _get_or_create_config(db)
    cfg_snapshot = {
        "lat_min": cfg.lat_min, "lat_max": cfg.lat_max,
        "lon_min": cfg.lon_min, "lon_max": cfg.lon_max,
        "run_time": cfg.run_time, "use_ensemble": cfg.use_ensemble,
        "parameters": cfg.parameters or '["tp"]',
    }
    background_tasks.add_task(_do_fetch_now, cfg_snapshot)

    return RedirectResponse("/ecmwf?fetching=1", status_code=303)
