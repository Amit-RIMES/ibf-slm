from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class GlofasRecord(Base):
    """GloFAS river discharge forecast record for a given forecast date."""
    __tablename__ = "glofas_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    forecast_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="GloFAS-v4")
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Bounding box of the data
    lat_min: Mapped[float] = mapped_column(Float, nullable=False)
    lat_max: Mapped[float] = mapped_column(Float, nullable=False)
    lon_min: Mapped[float] = mapped_column(Float, nullable=False)
    lon_max: Mapped[float] = mapped_column(Float, nullable=False)

    # Spatial statistics on river discharge (m³/s), ensemble mean
    discharge_min: Mapped[float] = mapped_column(Float, nullable=False)
    discharge_max: Mapped[float] = mapped_column(Float, nullable=False)
    discharge_mean: Mapped[float] = mapped_column(Float, nullable=False)

    # Lead time this record covers
    lead_days: Mapped[int] = mapped_column(Integer, nullable=False, default=10)

    # GeoJSON for Leaflet map (river network cells only)
    geojson: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    uploaded_by_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    uploaded_by: Mapped[Optional[object]] = relationship("User", foreign_keys=[uploaded_by_id])
