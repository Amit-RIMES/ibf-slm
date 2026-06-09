from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class ForecastUpload(Base):
    __tablename__ = "forecast_uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Spatial extent
    lat_min: Mapped[float] = mapped_column(Float, nullable=False)
    lat_max: Mapped[float] = mapped_column(Float, nullable=False)
    lon_min: Mapped[float] = mapped_column(Float, nullable=False)
    lon_max: Mapped[float] = mapped_column(Float, nullable=False)

    # Time range
    time_start: Mapped[str] = mapped_column(String(64), nullable=False)
    time_end: Mapped[str] = mapped_column(String(64), nullable=False)
    time_steps: Mapped[int] = mapped_column(Integer, nullable=False)

    # Precipitation stats (mm)
    precip_min: Mapped[float] = mapped_column(Float, nullable=False)
    precip_max: Mapped[float] = mapped_column(Float, nullable=False)
    precip_mean: Mapped[float] = mapped_column(Float, nullable=False)

    # GeoJSON grid for map rendering (stored as JSON string)
    geojson: Mapped[str] = mapped_column(Text, nullable=False)

    # Anomaly detection vs trailing history
    anomaly_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_anomaly: Mapped[Optional[bool]] = mapped_column(nullable=True)

    # Lead-time breakdown: JSON {"d1_5": {min,max,mean}, "d6_10": {...}, "d11_15": {...}}
    lead_time_stats: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Seasonal context: % deviation from same-month rolling mean (positive = above average)
    seasonal_anomaly_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Ensemble / probabilistic fields (null for deterministic forecasts)
    ensemble_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    precip_p10: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    precip_p25: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    precip_p50: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    precip_p75: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    precip_p90: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # JSON dict mapping threshold (str) → exceedance probability, e.g. {"50.0": 0.72}
    exceedance_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
