"""
Copernicus Data Store (CDS) admin configuration.
Manages CDS API key and sync settings for SEAS5, ERA5, and GloFAS.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.cds_config import CdsConfig
from app.models.glofas import GlofasRecord
from app.models.job_run import JobRun
from app.models.seasonal import SeasonalForecast
from app.models.observed_rainfall import ObservedRainfall

router = APIRouter(prefix="/cds")
templates = Jinja2Templates(directory="app/templates")

_FORBIDDEN = HTMLResponse(
    "<h1 style='font-family:system-ui;margin:3rem auto;max-width:400px'>403 — Admin access required</h1>",
    status_code=403,
)


async def _get_or_create_config(db: AsyncSession) -> CdsConfig:
    cfg = await db.scalar(select(CdsConfig).where(CdsConfig.id == 1))
    if not cfg:
        cfg = CdsConfig(
            id=1,
            api_url="https://cds.climate.copernicus.eu/api/v2",
            lat_min=0.0, lat_max=35.0, lon_min=60.0, lon_max=155.0,
        )
        db.add(cfg)
        await db.commit()
        await db.refresh(cfg)
    return cfg


@router.get("", response_class=HTMLResponse)
async def cds_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    cfg = await _get_or_create_config(db)

    # Recent SEAS5 seasonal records
    seas5_r = await db.execute(
        select(SeasonalForecast)
        .where(SeasonalForecast.source == "SEAS5")
        .order_by(SeasonalForecast.uploaded_at.desc())
        .limit(6)
    )
    seas5_records = seas5_r.scalars().all()

    # Recent ERA5 observed rainfall
    era5_r = await db.execute(
        select(ObservedRainfall)
        .where(ObservedRainfall.source == "ERA5")
        .order_by(ObservedRainfall.obs_date.desc())
        .limit(10)
    )
    era5_records = era5_r.scalars().all()

    # Recent GloFAS records
    glofas_r = await db.execute(
        select(GlofasRecord)
        .order_by(GlofasRecord.uploaded_at.desc())
        .limit(5)
    )
    glofas_records = glofas_r.scalars().all()

    # Job run history for CDS jobs
    runs_r = await db.execute(
        select(JobRun)
        .where(JobRun.job_name.in_(["seas5_sync", "era5_sync", "glofas_sync"]))
        .order_by(JobRun.started_at.desc())
        .limit(15)
    )
    runs = runs_r.scalars().all()

    return templates.TemplateResponse(
        request, "cds_config.html",
        {
            "user": user, "cfg": cfg,
            "seas5_records": seas5_records,
            "era5_records": era5_records,
            "glofas_records": glofas_records,
            "runs": runs,
            "fetching": request.query_params.get("fetching"),
        },
    )


@router.post("/config", response_class=HTMLResponse)
async def update_config(
    request: Request,
    api_key: str = Form(default=""),
    api_url: str = Form(default="https://cds.climate.copernicus.eu/api/v2"),
    lat_min: float = Form(0.0),
    lat_max: float = Form(35.0),
    lon_min: float = Form(60.0),
    lon_max: float = Form(155.0),
    seas5_enabled: str = Form(default="off"),
    seas5_sync_hour: int = Form(8),
    seas5_sync_minute: int = Form(0),
    seas5_lead_months: int = Form(6),
    era5_enabled: str = Form(default="off"),
    era5_sync_hour: int = Form(9),
    era5_sync_minute: int = Form(0),
    era5_lookback_days: int = Form(30),
    glofas_enabled: str = Form(default="off"),
    glofas_sync_hour: int = Form(11),
    glofas_sync_minute: int = Form(0),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    cfg = await _get_or_create_config(db)

    if api_key.strip():
        cfg.api_key = api_key.strip()
    cfg.api_url = api_url.strip() or "https://cds.climate.copernicus.eu/api/v2"
    cfg.lat_min = lat_min
    cfg.lat_max = lat_max
    cfg.lon_min = lon_min
    cfg.lon_max = lon_max

    cfg.seas5_enabled = seas5_enabled == "on"
    cfg.seas5_sync_hour = max(0, min(23, seas5_sync_hour))
    cfg.seas5_sync_minute = max(0, min(59, seas5_sync_minute))
    cfg.seas5_lead_months = max(1, min(6, seas5_lead_months))

    cfg.era5_enabled = era5_enabled == "on"
    cfg.era5_sync_hour = max(0, min(23, era5_sync_hour))
    cfg.era5_sync_minute = max(0, min(59, era5_sync_minute))
    cfg.era5_lookback_days = max(1, min(365, era5_lookback_days))

    cfg.glofas_enabled = glofas_enabled == "on"
    cfg.glofas_sync_hour = max(0, min(23, glofas_sync_hour))
    cfg.glofas_sync_minute = max(0, min(59, glofas_sync_minute))

    await db.commit()

    try:
        from app.scheduler import apply_cds_schedules
        await apply_cds_schedules()
    except Exception:
        pass

    return RedirectResponse("/cds", status_code=303)


# ── Fetch-now endpoints ────────────────────────────────────────────────────────

async def _do_fetch_seas5(cfg_snapshot: dict):
    from datetime import date
    from app.core.database import AsyncSessionLocal
    from app.core.seas5 import fetch_seas5
    from app.models.seasonal import SeasonalForecast

    started_at = datetime.now(timezone.utc)
    status, detail = "error", ""
    try:
        records = await fetch_seas5(
            api_url=cfg_snapshot["api_url"],
            api_key=cfg_snapshot["api_key"] or "",
            lat_min=cfg_snapshot["lat_min"], lat_max=cfg_snapshot["lat_max"],
            lon_min=cfg_snapshot["lon_min"], lon_max=cfg_snapshot["lon_max"],
            lead_months=cfg_snapshot["seas5_lead_months"],
        )
        if not records:
            detail = "SEAS5 fetch returned no records — check API key and region settings"
        else:
            async with AsyncSessionLocal() as db:
                added = 0
                for rec in records:
                    existing = await db.scalar(
                        select(SeasonalForecast).where(
                            SeasonalForecast.source == "SEAS5",
                            SeasonalForecast.issue_date == rec["issue_date"],
                            SeasonalForecast.valid_start == rec["valid_start"],
                        )
                    )
                    if not existing:
                        db.add(SeasonalForecast(**rec))
                        added += 1
                await db.commit()
            status = "ok"
            detail = f"Imported {added} of {len(records)} SEAS5 forecast months"
    except Exception as exc:
        detail = str(exc)[:512]
    _record_job(started_at, "seas5_sync", status, detail)
    _update_cds_status("seas5", status, detail)


async def _do_fetch_era5(cfg_snapshot: dict):
    from app.core.database import AsyncSessionLocal
    from app.core.era5 import fetch_era5
    from app.models.observed_rainfall import ObservedRainfall

    started_at = datetime.now(timezone.utc)
    status, detail = "error", ""
    try:
        records = await fetch_era5(
            api_url=cfg_snapshot["api_url"],
            api_key=cfg_snapshot["api_key"] or "",
            lat_min=cfg_snapshot["lat_min"], lat_max=cfg_snapshot["lat_max"],
            lon_min=cfg_snapshot["lon_min"], lon_max=cfg_snapshot["lon_max"],
            lookback_days=cfg_snapshot["era5_lookback_days"],
        )
        if not records:
            detail = "ERA5 fetch returned no records — check API key and region settings"
        else:
            async with AsyncSessionLocal() as db:
                added = 0
                for rec in records:
                    existing = await db.scalar(
                        select(ObservedRainfall).where(
                            ObservedRainfall.obs_date == rec["obs_date"],
                            ObservedRainfall.source == "ERA5",
                        )
                    )
                    if not existing:
                        db.add(ObservedRainfall(**rec))
                        added += 1
                await db.commit()
            status = "ok"
            detail = f"Imported {added} of {len(records)} ERA5 daily records"
    except Exception as exc:
        detail = str(exc)[:512]
    _record_job(started_at, "era5_sync", status, detail)
    _update_cds_status("era5", status, detail)


async def _do_fetch_glofas(cfg_snapshot: dict):
    from app.core.database import AsyncSessionLocal
    from app.core.glofas_fetch import fetch_glofas
    from app.models.glofas import GlofasRecord

    started_at = datetime.now(timezone.utc)
    status, detail = "error", ""
    try:
        data = await fetch_glofas(
            api_url=cfg_snapshot["api_url"],
            api_key=cfg_snapshot["api_key"] or "",
            lat_min=cfg_snapshot["lat_min"], lat_max=cfg_snapshot["lat_max"],
            lon_min=cfg_snapshot["lon_min"], lon_max=cfg_snapshot["lon_max"],
        )
        if data is None:
            detail = "GloFAS fetch returned None — check API key and region settings"
        else:
            async with AsyncSessionLocal() as db:
                db.add(GlofasRecord(**data))
                await db.commit()
            status = "ok"
            detail = f"GloFAS imported: mean={data['discharge_mean']} m³/s, max={data['discharge_max']} m³/s"
    except Exception as exc:
        detail = str(exc)[:512]
    _record_job(started_at, "glofas_sync", status, detail)
    _update_cds_status("glofas", status, detail)


def _record_job(started_at: datetime, job_name: str, status: str, detail: str):
    import asyncio
    from app.core.database import AsyncSessionLocal
    from app.models.job_run import JobRun

    async def _write():
        async with AsyncSessionLocal() as db:
            db.add(JobRun(
                job_name=job_name,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                status=status,
                detail=detail[:500],
            ))
            await db.commit()

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_write())
    except Exception:
        pass


def _update_cds_status(service: str, status: str, detail: str):
    import asyncio
    from app.core.database import AsyncSessionLocal

    async def _write():
        async with AsyncSessionLocal() as db:
            cfg = await db.scalar(select(CdsConfig).where(CdsConfig.id == 1))
            if cfg:
                setattr(cfg, f"{service}_last_run_at", datetime.now(timezone.utc))
                setattr(cfg, f"{service}_last_run_status", status)
                setattr(cfg, f"{service}_last_run_detail", detail[:512])
                await db.commit()

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_write())
    except Exception:
        pass


@router.post("/fetch-seas5", response_class=HTMLResponse)
async def fetch_seas5_now(
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
        "api_url": cfg.api_url, "api_key": cfg.api_key,
        "lat_min": cfg.lat_min, "lat_max": cfg.lat_max,
        "lon_min": cfg.lon_min, "lon_max": cfg.lon_max,
        "seas5_lead_months": cfg.seas5_lead_months,
    }
    background_tasks.add_task(_do_fetch_seas5, cfg_snapshot)
    return RedirectResponse("/cds?fetching=seas5", status_code=303)


@router.post("/fetch-era5", response_class=HTMLResponse)
async def fetch_era5_now(
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
        "api_url": cfg.api_url, "api_key": cfg.api_key,
        "lat_min": cfg.lat_min, "lat_max": cfg.lat_max,
        "lon_min": cfg.lon_min, "lon_max": cfg.lon_max,
        "era5_lookback_days": cfg.era5_lookback_days,
    }
    background_tasks.add_task(_do_fetch_era5, cfg_snapshot)
    return RedirectResponse("/cds?fetching=era5", status_code=303)


@router.post("/fetch-glofas", response_class=HTMLResponse)
async def fetch_glofas_now(
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
        "api_url": cfg.api_url, "api_key": cfg.api_key,
        "lat_min": cfg.lat_min, "lat_max": cfg.lat_max,
        "lon_min": cfg.lon_min, "lon_max": cfg.lon_max,
    }
    background_tasks.add_task(_do_fetch_glofas, cfg_snapshot)
    return RedirectResponse("/cds?fetching=glofas", status_code=303)
