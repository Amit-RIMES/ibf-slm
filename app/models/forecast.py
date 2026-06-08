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
