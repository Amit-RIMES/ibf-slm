from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ObservedRainfall(Base):
    __tablename__ = "observed_rainfall"
    __table_args__ = (UniqueConstraint("obs_date", "source", name="uq_obs_date_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    obs_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="CHIRPS")

    # Region used for spatial statistics
    lat_min: Mapped[float] = mapped_column(Float, nullable=False)
    lat_max: Mapped[float] = mapped_column(Float, nullable=False)
    lon_min: Mapped[float] = mapped_column(Float, nullable=False)
    lon_max: Mapped[float] = mapped_column(Float, nullable=False)

    # Spatial statistics over the region (mm/day)
    precip_mean: Mapped[float] = mapped_column(Float, nullable=False)
    precip_max: Mapped[float] = mapped_column(Float, nullable=False)
    precip_min: Mapped[float] = mapped_column(Float, nullable=False)
    wet_fraction: Mapped[float] = mapped_column(Float, nullable=False)  # fraction of pixels > 1 mm

    pixel_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_preliminary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # GeoJSON for Leaflet map (downsampled grid)
    geojson: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
