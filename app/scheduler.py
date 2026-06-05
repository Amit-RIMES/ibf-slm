import json
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal
from app.models.forecast import ForecastUpload
from app.models.sync import SyncConfig, SyncLog

logger = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler()
_JOB_ID = "daily_sync"


async def _run_daily_sync():
    from app.routers.forecasts import do_import

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(SyncConfig).where(SyncConfig.id == 1))
        cfg = result.scalar_one_or_none()
        if not cfg or not cfg.enabled:
            return

        sources = json.loads(cfg.sources or "[]")
        if not sources:
            return

        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        statuses = []

        for source in sources:
            try:
                forecast = await do_import(source, date_str, db)
                log = SyncLog(source=source, date=date_str, status="success",
                              message=f"Imported {forecast.filename}", forecast_id=forecast.id)
                statuses.append("success")
            except FileExistsError:
                log = SyncLog(source=source, date=date_str, status="skipped",
                              message="Already imported for this date")
                statuses.append("skipped")
            except Exception as exc:
                logger.error("Auto-sync failed for %s on %s: %s", source, date_str, exc)
                log = SyncLog(source=source, date=date_str, status="error", message=str(exc))
                statuses.append("error")
            db.add(log)

        if all(s == "skipped" for s in statuses):
            overall = "ok"
        elif "error" in statuses and "success" not in statuses:
            overall = "error"
        elif "error" in statuses:
            overall = "partial"
        else:
            overall = "ok"

        cfg.last_run_at = datetime.now(timezone.utc)
        cfg.last_run_status = overall
        await db.commit()

        deleted = await _cleanup_old_forecasts(db, cfg.retention_days)
        if deleted:
            logger.info("Retention cleanup: deleted %d old forecast(s)", deleted)


async def _cleanup_old_forecasts(db, retention_days: int) -> int:
    if not retention_days:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = await db.execute(
        delete(ForecastUpload).where(ForecastUpload.uploaded_at < cutoff)
    )
    deleted = result.rowcount
    if deleted:
        await db.commit()
    return deleted


def _schedule_job(hour: int, minute: int):
    if _scheduler.get_job(_JOB_ID):
        _scheduler.remove_job(_JOB_ID)
    _scheduler.add_job(
        _run_daily_sync,
        trigger="cron",
        hour=hour,
        minute=minute,
        id=_JOB_ID,
        replace_existing=True,
    )


async def apply_schedule():
    """Read config from DB and (re)schedule the daily job."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(SyncConfig).where(SyncConfig.id == 1))
        cfg = result.scalar_one_or_none()
        if cfg and cfg.enabled:
            _schedule_job(cfg.sync_hour, cfg.sync_minute)
        elif _scheduler.get_job(_JOB_ID):
            _scheduler.remove_job(_JOB_ID)


def start_scheduler():
    _scheduler.start()


def stop_scheduler():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
