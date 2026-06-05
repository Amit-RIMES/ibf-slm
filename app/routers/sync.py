import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.sync import SyncConfig, SyncLog
from app.routers.forecasts import SOURCES, do_import
from app.scheduler import apply_schedule, _cleanup_old_forecasts

router = APIRouter(prefix="/sync")
templates = Jinja2Templates(directory="app/templates")


async def _get_or_create_config(db: AsyncSession) -> SyncConfig:
    result = await db.execute(select(SyncConfig).where(SyncConfig.id == 1))
    cfg = result.scalar_one_or_none()
    if not cfg:
        cfg = SyncConfig(id=1, enabled=False, sources="[]", sync_hour=6, sync_minute=0, retention_days=90)
        db.add(cfg)
        await db.commit()
        await db.refresh(cfg)
    return cfg


@router.get("", response_class=HTMLResponse)
async def sync_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    cfg = await _get_or_create_config(db)
    selected_sources = json.loads(cfg.sources or "[]")

    logs_result = await db.execute(
        select(SyncLog).order_by(desc(SyncLog.run_at)).limit(50)
    )
    logs = logs_result.scalars().all()

    return templates.TemplateResponse("sync.html", {
        "request": request,
        "user": user,
        "cfg": cfg,
        "sources": SOURCES,
        "selected_sources": selected_sources,
        "logs": logs,
    })


@router.post("/config")
async def update_config(
    request: Request,
    enabled: str = Form(default="off"),
    sync_hour: int = Form(...),
    sync_minute: int = Form(...),
    sources: list[str] = Form(default=[]),
    retention_days: int = Form(default=90),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    cfg = await _get_or_create_config(db)
    cfg.enabled = enabled == "on"
    cfg.sync_hour = max(0, min(23, sync_hour))
    cfg.sync_minute = max(0, min(59, sync_minute))
    cfg.sources = json.dumps(sources)
    cfg.retention_days = max(0, retention_days)
    await db.commit()

    await apply_schedule()
    return RedirectResponse("/sync", status_code=303)


@router.post("/run")
async def run_now(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    cfg = await _get_or_create_config(db)
    sources = json.loads(cfg.sources or "[]")
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")

    for source in sources:
        try:
            forecast = await do_import(source, date_str, db)
            log = SyncLog(source=source, date=date_str, status="success",
                          message=f"Imported {forecast.filename}", forecast_id=forecast.id)
        except FileExistsError:
            log = SyncLog(source=source, date=date_str, status="skipped",
                          message="Already imported for this date")
        except Exception as exc:
            log = SyncLog(source=source, date=date_str, status="error", message=str(exc))
        db.add(log)

    cfg.last_run_at = datetime.now(timezone.utc)
    await db.commit()

    await _cleanup_old_forecasts(db, cfg.retention_days)

    return RedirectResponse("/sync", status_code=303)
