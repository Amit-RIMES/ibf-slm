import math
from typing import TYPE_CHECKING

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.models.forecast import ForecastUpload

# Minimum history size before scoring; below this there's not enough baseline.
_MIN_HISTORY = 5
# How many prior forecasts to use as the baseline window.
_WINDOW = 60
# Z-score threshold above which a forecast is flagged as anomalous.
_THRESHOLD = 2.0


async def compute_anomaly(forecast: "ForecastUpload", db: AsyncSession) -> None:
    """Score forecast.precip_mean against trailing history and set anomaly fields.

    Uses up to _WINDOW prior forecasts (excluding this one) as the baseline.
    Sets forecast.anomaly_score (z-score, or None if insufficient data) and
    forecast.is_anomaly (True when z-score > _THRESHOLD).
    Commits the update.
    """
    from app.models.forecast import ForecastUpload

    result = await db.execute(
        select(ForecastUpload.precip_mean)
        .where(ForecastUpload.id != forecast.id)
        .order_by(desc(ForecastUpload.uploaded_at))
        .limit(_WINDOW)
    )
    history = [row[0] for row in result.all()]

    if len(history) < _MIN_HISTORY:
        forecast.anomaly_score = None
        forecast.is_anomaly = False
        await db.commit()
        return

    mean = sum(history) / len(history)
    variance = sum((x - mean) ** 2 for x in history) / len(history)
    stddev = math.sqrt(variance)

    if stddev < 0.01:
        forecast.anomaly_score = 0.0
        forecast.is_anomaly = False
        await db.commit()
        return

    z = (forecast.precip_mean - mean) / stddev
    forecast.anomaly_score = round(z, 2)
    forecast.is_anomaly = z > _THRESHOLD
    await db.commit()
