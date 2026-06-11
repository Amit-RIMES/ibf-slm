from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.forecast import ForecastUpload
from app.models.observed_rainfall import ObservedRainfall


async def check_data_gaps(db: AsyncSession) -> dict:
    """Return gap status for CHIRPS and forecast data streams."""
    now = datetime.now(timezone.utc)

    # CHIRPS gap
    chirps_r = await db.execute(
        select(ObservedRainfall.obs_date).order_by(ObservedRainfall.obs_date.desc()).limit(1)
    )
    last_chirps_date = chirps_r.scalar_one_or_none()
    chirps_gap_days = (now.date() - last_chirps_date).days if last_chirps_date else None
    chirps_alert = chirps_gap_days is not None and chirps_gap_days >= settings.DATA_GAP_CHIRPS_DAYS

    # Forecast gap
    fc_r = await db.execute(
        select(ForecastUpload.uploaded_at).order_by(ForecastUpload.uploaded_at.desc()).limit(1)
    )
    last_fc_at = fc_r.scalar_one_or_none()
    if last_fc_at is not None:
        fc_aware = last_fc_at if last_fc_at.tzinfo else last_fc_at.replace(tzinfo=timezone.utc)
        forecast_gap_days = int((now - fc_aware).total_seconds() / 86400)
    else:
        forecast_gap_days = None
    forecast_alert = (
        forecast_gap_days is not None and forecast_gap_days >= settings.DATA_GAP_FORECAST_DAYS
    )

    return {
        "chirps_gap_days": chirps_gap_days,
        "chirps_alert": chirps_alert,
        "last_chirps_date": last_chirps_date,
        "forecast_gap_days": forecast_gap_days,
        "forecast_alert": forecast_alert,
        "last_forecast_at": last_fc_at,
        "any_alert": chirps_alert or forecast_alert,
    }
