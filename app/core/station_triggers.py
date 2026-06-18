"""Evaluate station-variable trigger rules against recently saved observations."""
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.station import StationObservation
from app.models.trigger import STATION_VARIABLES, Trigger, TriggerActivation

logger = logging.getLogger(__name__)

_OPS = {
    "gt":  lambda v, t: v > t,
    "gte": lambda v, t: v >= t,
    "lt":  lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
}


async def evaluate_station_triggers(db: AsyncSession, obs_date: date) -> int:
    """Check station-variable triggers against new observations. Returns count fired."""
    result = await db.execute(
        select(Trigger).where(
            Trigger.is_active == True,  # noqa: E712
            Trigger.variable.in_(STATION_VARIABLES),
        )
    )
    triggers = result.scalars().all()
    if not triggers:
        return 0

    # Aggregate max precip over the relevant windows ending on obs_date
    since_24h = obs_date - timedelta(days=1)
    since_48h = obs_date - timedelta(days=2)

    precip_24h = await db.scalar(
        select(func.max(StationObservation.precip_mm)).where(
            StationObservation.obs_date >= since_24h,
            StationObservation.precip_mm.is_not(None),
        )
    ) or 0.0

    precip_48h = await db.scalar(
        select(func.max(StationObservation.precip_mm)).where(
            StationObservation.obs_date >= since_48h,
            StationObservation.precip_mm.is_not(None),
        )
    ) or 0.0

    var_values = {
        "station_precip_24h": precip_24h,
        "station_precip_48h": precip_48h,
    }

    now = datetime.now(timezone.utc)
    cooldown_cutoff = now - timedelta(hours=settings.TRIGGER_COOLDOWN_HOURS)
    fired = 0

    for trigger in triggers:
        value = var_values.get(trigger.variable, 0.0)
        op_fn = _OPS.get(trigger.operator)
        if op_fn is None or not op_fn(value, trigger.threshold):
            continue

        # Respect cooldown — check before db.add to avoid autoflush issues
        recent = await db.execute(
            select(TriggerActivation.id).where(
                TriggerActivation.trigger_id == trigger.id,
                TriggerActivation.triggered_at >= cooldown_cutoff,
            ).limit(1)
        )
        if recent.scalar_one_or_none() is not None:
            logger.debug("Trigger %d in cooldown — suppressed", trigger.id)
            continue

        db.add(TriggerActivation(
            trigger_id=trigger.id,
            forecast_id=None,
            value=value,
            status="active",
        ))
        fired += 1
        logger.info("Station trigger %d fired: %s=%.2f %s %.2f",
                    trigger.id, trigger.variable, value, trigger.operator, trigger.threshold)

    if fired:
        await db.commit()

    return fired
