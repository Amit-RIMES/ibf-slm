import json
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.sync import SyncConfig, SyncLog
from app.models.trigger import TriggerActivation
from app.models.user import User

logger = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler()
_JOB_ID = "daily_sync"
_ESCALATION_JOB_ID = "alert_escalation"
_DIGEST_JOB_ID = "weekly_digest"
_CHIRPS_JOB_ID = "chirps_sync"


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

        if overall == "error":
            await _check_and_notify_sync_failures(db, cfg.id)

        deleted = await _cleanup_old_forecasts(db, cfg.retention_days)
        if deleted:
            logger.info("Retention cleanup: deleted %d old forecast(s)", deleted)


async def _check_and_notify_sync_failures(db, config_id: int) -> None:
    from app.core.email import send_sync_failure_email
    threshold = settings.SMTP_FAILURE_ALERT_AFTER
    recent = await db.execute(
        select(SyncLog.status)
        .order_by(SyncLog.id.desc())
        .limit(threshold)
    )
    statuses = [r[0] for r in recent.all()]
    if len(statuses) == threshold and all(s == "error" for s in statuses):
        admins = await db.execute(
            select(User.email).where(User.role == "admin", User.is_active == True)  # noqa: E712
        )
        admin_emails = [r[0] for r in admins.all()]
        import asyncio
        asyncio.create_task(send_sync_failure_email(admin_emails, threshold, settings.APP_BASE_URL))


async def _run_alert_escalation() -> None:
    from app.core.email import send_escalation_email
    from app.models.trigger import Trigger

    threshold = timedelta(hours=settings.ALERT_ESCALATION_HOURS)
    cutoff = datetime.now(timezone.utc) - threshold

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TriggerActivation)
            .where(
                TriggerActivation.status == "active",
                TriggerActivation.triggered_at <= cutoff,
                (TriggerActivation.last_escalated_at == None)  # noqa: E711
                | (TriggerActivation.last_escalated_at <= cutoff),
            )
        )
        pending = result.scalars().all()
        if not pending:
            return

        admins = await db.execute(
            select(User.email).where(User.role == "admin", User.is_active == True)  # noqa: E712
        )
        admin_emails = [r[0] for r in admins.all()]
        if not admin_emails:
            return

        for activation in pending:
            trigger_res = await db.execute(select(Trigger).where(Trigger.id == activation.trigger_id))
            trigger = trigger_res.scalar_one_or_none()
            if not trigger:
                continue
            hours_unacked = int((datetime.now(timezone.utc) - activation.triggered_at).total_seconds() / 3600)
            import asyncio
            asyncio.create_task(
                send_escalation_email(admin_emails, activation, trigger, hours_unacked, settings.APP_BASE_URL)
            )
            activation.last_escalated_at = datetime.now(timezone.utc)

        await db.commit()
        logger.info("Escalated %d unacknowledged activation(s)", len(pending))


async def _run_weekly_digest() -> None:
    from app.core.email import send_weekly_digest_email

    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    week_label = f"{week_start.strftime('%d %b')} – {now.strftime('%d %b %Y')}"

    async with AsyncSessionLocal() as db:
        admins = await db.execute(
            select(User.email).where(User.role == "admin", User.is_active == True)  # noqa: E712
        )
        admin_emails = [r[0] for r in admins.all()]
        if not admin_emails:
            return

        n_act = await db.scalar(
            select(func.count()).select_from(TriggerActivation)
            .where(TriggerActivation.triggered_at >= week_start)
        )
        n_ack = await db.scalar(
            select(func.count()).select_from(TriggerActivation)
            .where(
                TriggerActivation.triggered_at >= week_start,
                TriggerActivation.status == "acknowledged",
            )
        )
        n_impacts = await db.scalar(
            select(func.count()).select_from(ImpactRecord)
            .where(ImpactRecord.created_at >= week_start)
        )
        n_forecasts = await db.scalar(
            select(func.count()).select_from(ForecastUpload)
            .where(ForecastUpload.uploaded_at >= week_start)
        )

        # Hazard breakdown
        from app.models.trigger import Trigger
        hazard_rows = await db.execute(
            select(Trigger.hazard_type, func.count(TriggerActivation.id).label("n"))
            .join(TriggerActivation, TriggerActivation.trigger_id == Trigger.id)
            .where(TriggerActivation.triggered_at >= week_start)
            .group_by(Trigger.hazard_type)
            .order_by(func.count(TriggerActivation.id).desc())
            .limit(5)
        )
        top_hazards = [(r[0], r[1]) for r in hazard_rows.all()]

        # Coverage gaps: configured sources that had no successful sync this week
        cfg_res = await db.execute(select(SyncConfig).where(SyncConfig.id == 1))
        cfg = cfg_res.scalar_one_or_none()
        all_sources = json.loads(cfg.sources or "[]") if cfg else []
        synced_sources_res = await db.execute(
            select(SyncLog.source).where(
                SyncLog.status == "success",
                SyncLog.date >= week_start.strftime("%Y%m%d"),
            ).distinct()
        )
        synced = {r[0] for r in synced_sources_res.all()}
        coverage_gaps = [s for s in all_sources if s not in synced]

        stats = {
            "n_activations": n_act or 0,
            "n_acknowledged": n_ack or 0,
            "n_impacts": n_impacts or 0,
            "n_forecasts": n_forecasts or 0,
            "top_hazards": top_hazards,
            "coverage_gaps": coverage_gaps,
            "week_label": week_label,
        }

    import asyncio
    asyncio.create_task(send_weekly_digest_email(admin_emails, stats, settings.APP_BASE_URL))
    logger.info("Weekly digest dispatched to %d admin(s)", len(admin_emails))


async def _run_chirps_sync():
    if not settings.CHIRPS_ENABLED:
        return
    from app.core.chirps import sync_recent_days
    async with AsyncSessionLocal() as db:
        ingested = await sync_recent_days(
            db,
            lookback_days=settings.CHIRPS_LOOKBACK_DAYS,
            lat_min=settings.CHIRPS_LAT_MIN,
            lat_max=settings.CHIRPS_LAT_MAX,
            lon_min=settings.CHIRPS_LON_MIN,
            lon_max=settings.CHIRPS_LON_MAX,
        )
    if ingested:
        logger.info("CHIRPS sync: ingested %d new day(s): %s", len(ingested), ingested)
    else:
        logger.debug("CHIRPS sync: no new data")


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
    """Read config from DB and (re)schedule all jobs."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(SyncConfig).where(SyncConfig.id == 1))
        cfg = result.scalar_one_or_none()
        if cfg and cfg.enabled:
            _schedule_job(cfg.sync_hour, cfg.sync_minute)
        elif _scheduler.get_job(_JOB_ID):
            _scheduler.remove_job(_JOB_ID)

    # Alert escalation — run every hour
    if not _scheduler.get_job(_ESCALATION_JOB_ID):
        _scheduler.add_job(
            _run_alert_escalation,
            trigger="interval",
            hours=1,
            id=_ESCALATION_JOB_ID,
            replace_existing=True,
        )

    # Weekly digest — Monday morning
    if not _scheduler.get_job(_DIGEST_JOB_ID):
        _scheduler.add_job(
            _run_weekly_digest,
            trigger="cron",
            day_of_week=settings.WEEKLY_DIGEST_DAY,
            hour=settings.WEEKLY_DIGEST_HOUR,
            minute=0,
            id=_DIGEST_JOB_ID,
            replace_existing=True,
        )

    # CHIRPS observed rainfall — daily at 02:30 UTC (data ~1-day lag)
    if not _scheduler.get_job(_CHIRPS_JOB_ID):
        _scheduler.add_job(
            _run_chirps_sync,
            trigger="cron",
            hour=2,
            minute=30,
            id=_CHIRPS_JOB_ID,
            replace_existing=True,
        )


def start_scheduler():
    _scheduler.start()


def stop_scheduler():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
