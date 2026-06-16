import json
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, func, select

from app.core.background import enqueue
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.bulletin_schedule import BulletinSchedule
from app.models.cds_config import CdsConfig
from app.models.ecmwf_config import EcmwfConfig
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.sync import SyncConfig, SyncLog
from app.models.trigger import TriggerActivation
from app.models.user import User

logger = logging.getLogger(__name__)


async def _record_job(job_name: str, status: str, started_at: datetime, detail: str = "") -> None:
    try:
        from app.models.job_run import JobRun
        async with AsyncSessionLocal() as db:
            db.add(JobRun(
                job_name=job_name,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                status=status,
                detail=detail[:500],
            ))
            await db.commit()
    except Exception:
        pass  # recording must never break the actual job


_scheduler = AsyncIOScheduler()
_JOB_ID = "daily_sync"
_ESCALATION_JOB_ID = "alert_escalation"
_DIGEST_JOB_ID = "weekly_digest"
_CHIRPS_JOB_ID = "chirps_sync"
_GAP_CHECK_JOB_ID = "data_gap_check"
_BULLETIN_JOB_ID = "bulletin_email"
_ECMWF_JOB_ID = "ecmwf_sync"
_SEAS5_JOB_ID = "seas5_sync"
_ERA5_JOB_ID = "era5_sync"
_GLOFAS_JOB_ID = "glofas_sync"


async def _run_daily_sync():
    _started = datetime.now(timezone.utc)
    from app.routers.forecasts import do_import

    try:
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

        await _record_job("daily_sync", overall, _started)
    except Exception as exc:
        await _record_job("daily_sync", "error", _started, str(exc))
        raise


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
        enqueue(send_sync_failure_email(admin_emails, threshold, settings.APP_BASE_URL))


async def _run_alert_escalation() -> None:
    _started = datetime.now(timezone.utc)
    from app.core.email import send_escalation_email
    from app.models.trigger import Trigger

    try:
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
                await _record_job("alert_escalation", "skipped", _started, "no pending escalations")
                return

            admins = await db.execute(
                select(User.email).where(User.role == "admin", User.is_active == True)  # noqa: E712
            )
            admin_emails = [r[0] for r in admins.all()]
            if not admin_emails:
                await _record_job("alert_escalation", "skipped", _started, "no admin emails")
                return

            for activation in pending:
                trigger_res = await db.execute(select(Trigger).where(Trigger.id == activation.trigger_id))
                trigger = trigger_res.scalar_one_or_none()
                if not trigger:
                    continue
                hours_unacked = int((datetime.now(timezone.utc) - activation.triggered_at).total_seconds() / 3600)
                enqueue(
                    send_escalation_email(admin_emails, activation, trigger, hours_unacked, settings.APP_BASE_URL)
                )
                activation.last_escalated_at = datetime.now(timezone.utc)

            await db.commit()
            logger.info("Escalated %d unacknowledged activation(s)", len(pending))
        await _record_job("alert_escalation", "ok", _started, f"escalated {len(pending)}")
    except Exception as exc:
        await _record_job("alert_escalation", "error", _started, str(exc))
        raise


async def _run_weekly_digest() -> None:
    _started = datetime.now(timezone.utc)
    from app.core.email import send_weekly_digest_email

    try:
        now = datetime.now(timezone.utc)
        week_start = now - timedelta(days=7)
        week_label = f"{week_start.strftime('%d %b')} – {now.strftime('%d %b %Y')}"

        async with AsyncSessionLocal() as db:
            admins = await db.execute(
                select(User.email).where(User.role == "admin", User.is_active == True)  # noqa: E712
            )
            admin_emails = [r[0] for r in admins.all()]
            if not admin_emails:
                await _record_job("weekly_digest", "skipped", _started, "no admin emails")
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

        enqueue(send_weekly_digest_email(admin_emails, stats, settings.APP_BASE_URL))
        logger.info("Weekly digest dispatched to %d admin(s)", len(admin_emails))
        await _record_job("weekly_digest", "ok", _started, f"sent to {len(admin_emails)}")
    except Exception as exc:
        await _record_job("weekly_digest", "error", _started, str(exc))
        raise


async def _run_chirps_sync():
    _started = datetime.now(timezone.utc)
    if not settings.CHIRPS_ENABLED:
        return
    try:
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
            from app.core.spi import recompute_and_evaluate
            from app.core.risk import compute_and_record_risk_score
            async with AsyncSessionLocal() as db:
                await recompute_and_evaluate(db)
            async with AsyncSessionLocal() as db:
                await compute_and_record_risk_score(db, source="CHIRPS")
            await _record_job("chirps_sync", "ok", _started, f"ingested {len(ingested)} days")
        else:
            logger.debug("CHIRPS sync: no new data")
            await _record_job("chirps_sync", "skipped", _started, "no new data")
    except Exception as exc:
        await _record_job("chirps_sync", "error", _started, str(exc))
        raise


async def _run_gap_check():
    _started = datetime.now(timezone.utc)
    from app.core.email import send_data_gap_email
    from app.core.gaps import check_data_gaps

    try:
        async with AsyncSessionLocal() as db:
            gaps = await check_data_gaps(db)
            if not gaps["any_alert"]:
                await _record_job("data_gap_check", "skipped", _started, "no gaps")
                return

            now = datetime.now(timezone.utc)
            cooldown = timedelta(hours=settings.DATA_GAP_ALERT_COOLDOWN_HOURS)

            cfg_r = await db.execute(select(SyncConfig).where(SyncConfig.id == 1))
            cfg = cfg_r.scalar_one_or_none()
            if cfg is None:
                cfg = SyncConfig(id=1, enabled=False, sources="[]")
                db.add(cfg)
                await db.flush()

            def _since(ts):
                if ts is None:
                    return None
                aware = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                return now - aware

            should_alert_chirps = gaps["chirps_alert"] and (
                cfg.last_chirps_gap_alert_at is None
                or _since(cfg.last_chirps_gap_alert_at) >= cooldown
            )
            should_alert_forecast = gaps["forecast_alert"] and (
                cfg.last_forecast_gap_alert_at is None
                or _since(cfg.last_forecast_gap_alert_at) >= cooldown
            )

            if not should_alert_chirps and not should_alert_forecast:
                await _record_job("data_gap_check", "skipped", _started, "within cooldown")
                return

            # Only include alert types that are within their cooldown window
            active_gaps = dict(gaps)
            if not should_alert_chirps:
                active_gaps["chirps_alert"] = False
            if not should_alert_forecast:
                active_gaps["forecast_alert"] = False

            admins = await db.execute(
                select(User.email).where(User.role == "admin", User.is_active == True)  # noqa: E712
            )
            admin_emails = [r[0] for r in admins.all()]
            if admin_emails:
                enqueue(send_data_gap_email(admin_emails, active_gaps, settings.APP_BASE_URL))

            if cfg:
                if should_alert_chirps:
                    cfg.last_chirps_gap_alert_at = now
                if should_alert_forecast:
                    cfg.last_forecast_gap_alert_at = now
                await db.commit()

            logger.warning(
                "Data gap alert sent: CHIRPS=%s (%s days), forecast=%s (%s days)",
                gaps["chirps_alert"], gaps["chirps_gap_days"],
                gaps["forecast_alert"], gaps["forecast_gap_days"],
            )
        await _record_job("data_gap_check", "ok", _started, "alert sent")
    except Exception as exc:
        await _record_job("data_gap_check", "error", _started, str(exc))
        raise


async def _run_bulletin_email() -> None:
    _started = datetime.now(timezone.utc)
    from app.core.email import send_bulletin_email
    from app.models.bulletin_schedule import BulletinSchedule, BulletinSubscriber
    from app.routers.bulletin import _build_bulletin_context, _format_subject, _render_bulletin_html

    try:
        async with AsyncSessionLocal() as db:
            cfg = await db.scalar(select(BulletinSchedule).where(BulletinSchedule.id == 1))
            if not cfg or not cfg.enabled:
                await _record_job("bulletin_email", "skipped", _started, "not enabled")
                return

            subscribers_r = await db.execute(
                select(BulletinSubscriber).where(BulletinSubscriber.is_active == True)  # noqa: E712
            )
            recipients = [s.email for s in subscribers_r.scalars().all()]
            if not recipients:
                await _record_job("bulletin_email", "skipped", _started, "no recipients")
                return

            ctx = await _build_bulletin_context(db, cfg.source, cfg.days)
            html = _render_bulletin_html(ctx)
            subject = _format_subject(cfg.subject_template, ctx["now"])

            sent = await send_bulletin_email(recipients, subject, html)

            cfg.last_sent_at = datetime.now(timezone.utc)
            await db.commit()

        logger.info("Bulletin email dispatched to %d/%d recipient(s)", sent, len(recipients))
        await _record_job("bulletin_email", "ok", _started, f"sent to {sent}/{len(recipients)}")
    except Exception as exc:
        await _record_job("bulletin_email", "error", _started, str(exc))
        raise


async def apply_bulletin_schedule() -> None:
    """(Re)schedule the bulletin job from DB config. Safe to call at any time."""
    async with AsyncSessionLocal() as db:
        cfg = await db.scalar(select(BulletinSchedule).where(BulletinSchedule.id == 1))

    if _scheduler.get_job(_BULLETIN_JOB_ID):
        _scheduler.remove_job(_BULLETIN_JOB_ID)

    if not cfg or not cfg.enabled:
        return

    if cfg.frequency == "weekly":
        _scheduler.add_job(
            _run_bulletin_email,
            trigger="cron",
            day_of_week=cfg.day_of_week,
            hour=cfg.hour,
            minute=0,
            id=_BULLETIN_JOB_ID,
            replace_existing=True,
        )
    else:
        _scheduler.add_job(
            _run_bulletin_email,
            trigger="cron",
            hour=cfg.hour,
            minute=0,
            id=_BULLETIN_JOB_ID,
            replace_existing=True,
        )


async def _run_ecmwf_sync() -> None:
    _started = datetime.now(timezone.utc)
    try:
        from app.core.anomaly import compute_anomaly
        from app.core.ecmwf_opendata import fetch_ecmwf_forecast
        from app.routers.triggers import evaluate_triggers
        from sqlalchemy import select as _sel

        async with AsyncSessionLocal() as db:
            cfg = await db.scalar(_sel(EcmwfConfig).where(EcmwfConfig.id == 1))
            if not cfg or not cfg.enabled:
                await _record_job(_ECMWF_JOB_ID, "skipped", _started, "not enabled")
                return

            data = await fetch_ecmwf_forecast(
                lat_min=cfg.lat_min, lat_max=cfg.lat_max,
                lon_min=cfg.lon_min, lon_max=cfg.lon_max,
                run_time=cfg.run_time, use_ensemble=cfg.use_ensemble,
            )

        if data is None:
            async with AsyncSessionLocal() as db:
                cfg = await db.scalar(_sel(EcmwfConfig).where(EcmwfConfig.id == 1))
                if cfg:
                    cfg.last_run_at = datetime.now(timezone.utc)
                    cfg.last_run_status = "error"
                    cfg.last_run_detail = "fetch returned None — see logs"
                    await db.commit()
            await _record_job(_ECMWF_JOB_ID, "error", _started, "fetch returned None")
            return

        async with AsyncSessionLocal() as db:
            existing = await db.scalar(
                _sel(ForecastUpload).where(ForecastUpload.filename == data["filename"])
            )
            if existing:
                detail = f"Already imported: {data['filename']}"
                await _record_job(_ECMWF_JOB_ID, "skipped", _started, detail)
                return

            lead_time_stats = data.pop("lead_time_stats", None)
            forecast = ForecastUpload(lead_time_stats=lead_time_stats, **data)
            db.add(forecast)
            await db.commit()
            await db.refresh(forecast)
            await compute_anomaly(forecast, db)
            await evaluate_triggers(forecast, db)
            detail = f"Imported {forecast.filename} (mean={forecast.precip_mean} mm)"
            logger.info("ECMWF sync: %s", detail)

            cfg = await db.scalar(_sel(EcmwfConfig).where(EcmwfConfig.id == 1))
            if cfg:
                cfg.last_run_at = datetime.now(timezone.utc)
                cfg.last_run_status = "ok"
                cfg.last_run_detail = detail[:512]
                await db.commit()

        await _record_job(_ECMWF_JOB_ID, "ok", _started, detail)
    except Exception as exc:
        await _record_job(_ECMWF_JOB_ID, "error", _started, str(exc))
        raise


async def apply_ecmwf_schedule() -> None:
    """Read EcmwfConfig from DB and (re)schedule the ecmwf_sync job."""
    async with AsyncSessionLocal() as db:
        cfg = await db.scalar(select(EcmwfConfig).where(EcmwfConfig.id == 1))

    if _scheduler.get_job(_ECMWF_JOB_ID):
        _scheduler.remove_job(_ECMWF_JOB_ID)

    if not cfg or not cfg.enabled:
        return

    _scheduler.add_job(
        _run_ecmwf_sync,
        trigger="cron",
        hour=cfg.sync_hour,
        minute=cfg.sync_minute,
        id=_ECMWF_JOB_ID,
        replace_existing=True,
    )
    logger.info("ECMWF sync scheduled at %02d:%02d UTC", cfg.sync_hour, cfg.sync_minute)


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

    # Data gap check — daily at 08:00 UTC (after CHIRPS sync window)
    if not _scheduler.get_job(_GAP_CHECK_JOB_ID):
        _scheduler.add_job(
            _run_gap_check,
            trigger="cron",
            hour=8,
            minute=0,
            id=_GAP_CHECK_JOB_ID,
            replace_existing=True,
        )

    # Bulletin email — schedule driven by DB config
    await apply_bulletin_schedule()

    # ECMWF Open Data IFS — schedule driven by DB config
    await apply_ecmwf_schedule()

    # CDS (SEAS5 / ERA5 / GloFAS) — schedule driven by DB config
    await apply_cds_schedules()


# ── CDS sync jobs ──────────────────────────────────────────────────────────────

async def _run_seas5_sync() -> None:
    _started = datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as db:
            cfg = await db.scalar(select(CdsConfig).where(CdsConfig.id == 1))
        if not cfg or not cfg.seas5_enabled or not cfg.api_key:
            return

        from app.core.seas5 import fetch_seas5
        from app.models.seasonal import SeasonalForecast

        records = await fetch_seas5(
            api_url=cfg.api_url, api_key=cfg.api_key,
            lat_min=cfg.lat_min, lat_max=cfg.lat_max,
            lon_min=cfg.lon_min, lon_max=cfg.lon_max,
            lead_months=cfg.seas5_lead_months,
        )
        added = 0
        if records:
            async with AsyncSessionLocal() as db:
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

        detail = f"Imported {added}/{len(records)} SEAS5 months" if records else "No records returned"
        await _record_job(_SEAS5_JOB_ID, "ok" if records else "error", _started, detail)
        async with AsyncSessionLocal() as db:
            cfg = await db.scalar(select(CdsConfig).where(CdsConfig.id == 1))
            if cfg:
                cfg.seas5_last_run_at = datetime.now(timezone.utc)
                cfg.seas5_last_run_status = "ok" if records else "error"
                cfg.seas5_last_run_detail = detail[:512]
                await db.commit()
    except Exception as exc:
        await _record_job(_SEAS5_JOB_ID, "error", _started, str(exc))


async def _run_era5_sync() -> None:
    _started = datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as db:
            cfg = await db.scalar(select(CdsConfig).where(CdsConfig.id == 1))
        if not cfg or not cfg.era5_enabled or not cfg.api_key:
            return

        from app.core.era5 import fetch_era5
        from app.models.observed_rainfall import ObservedRainfall

        records = await fetch_era5(
            api_url=cfg.api_url, api_key=cfg.api_key,
            lat_min=cfg.lat_min, lat_max=cfg.lat_max,
            lon_min=cfg.lon_min, lon_max=cfg.lon_max,
            lookback_days=cfg.era5_lookback_days,
        )
        added = 0
        if records:
            async with AsyncSessionLocal() as db:
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

        detail = f"Imported {added}/{len(records)} ERA5 days" if records else "No records returned"
        await _record_job(_ERA5_JOB_ID, "ok" if records else "error", _started, detail)
        async with AsyncSessionLocal() as db:
            cfg = await db.scalar(select(CdsConfig).where(CdsConfig.id == 1))
            if cfg:
                cfg.era5_last_run_at = datetime.now(timezone.utc)
                cfg.era5_last_run_status = "ok" if records else "error"
                cfg.era5_last_run_detail = detail[:512]
                await db.commit()
    except Exception as exc:
        await _record_job(_ERA5_JOB_ID, "error", _started, str(exc))


async def _run_glofas_sync() -> None:
    _started = datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as db:
            cfg = await db.scalar(select(CdsConfig).where(CdsConfig.id == 1))
        if not cfg or not cfg.glofas_enabled or not cfg.api_key:
            return

        from app.core.glofas_fetch import fetch_glofas
        from app.models.glofas import GlofasRecord

        data = await fetch_glofas(
            api_url=cfg.api_url, api_key=cfg.api_key,
            lat_min=cfg.lat_min, lat_max=cfg.lat_max,
            lon_min=cfg.lon_min, lon_max=cfg.lon_max,
        )
        status = "error"
        detail = "Fetch returned None"
        if data:
            async with AsyncSessionLocal() as db:
                db.add(GlofasRecord(**data))
                await db.commit()
            status = "ok"
            detail = f"GloFAS: mean={data['discharge_mean']} m³/s, max={data['discharge_max']} m³/s"

        await _record_job(_GLOFAS_JOB_ID, status, _started, detail)
        async with AsyncSessionLocal() as db:
            cfg = await db.scalar(select(CdsConfig).where(CdsConfig.id == 1))
            if cfg:
                cfg.glofas_last_run_at = datetime.now(timezone.utc)
                cfg.glofas_last_run_status = status
                cfg.glofas_last_run_detail = detail[:512]
                await db.commit()
    except Exception as exc:
        await _record_job(_GLOFAS_JOB_ID, "error", _started, str(exc))


async def apply_cds_schedules() -> None:
    """Read CdsConfig from DB and (re)schedule SEAS5, ERA5, GloFAS jobs."""
    async with AsyncSessionLocal() as db:
        cfg = await db.scalar(select(CdsConfig).where(CdsConfig.id == 1))

    for job_id in (_SEAS5_JOB_ID, _ERA5_JOB_ID, _GLOFAS_JOB_ID):
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)

    if not cfg:
        return

    if cfg.seas5_enabled and cfg.api_key:
        _scheduler.add_job(
            _run_seas5_sync, trigger="cron",
            hour=cfg.seas5_sync_hour, minute=cfg.seas5_sync_minute,
            id=_SEAS5_JOB_ID, replace_existing=True,
        )
        logger.info("SEAS5 sync scheduled at %02d:%02d UTC", cfg.seas5_sync_hour, cfg.seas5_sync_minute)

    if cfg.era5_enabled and cfg.api_key:
        _scheduler.add_job(
            _run_era5_sync, trigger="cron",
            hour=cfg.era5_sync_hour, minute=cfg.era5_sync_minute,
            id=_ERA5_JOB_ID, replace_existing=True,
        )
        logger.info("ERA5 sync scheduled at %02d:%02d UTC", cfg.era5_sync_hour, cfg.era5_sync_minute)

    if cfg.glofas_enabled and cfg.api_key:
        _scheduler.add_job(
            _run_glofas_sync, trigger="cron",
            hour=cfg.glofas_sync_hour, minute=cfg.glofas_sync_minute,
            id=_GLOFAS_JOB_ID, replace_existing=True,
        )
        logger.info("GloFAS sync scheduled at %02d:%02d UTC", cfg.glofas_sync_hour, cfg.glofas_sync_minute)


def start_scheduler():
    _scheduler.start()


def stop_scheduler():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
