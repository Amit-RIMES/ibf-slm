from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class SeasonalForecast(Base):
    __tablename__ = "seasonal_forecasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    valid_start: Mapped[date] = mapped_column(Date, nullable=False)
    valid_end: Mapped[date] = mapped_column(Date, nullable=False)
    variable: Mapped[str] = mapped_column(String(16), nullable=False, default="precip")

    # IRI-style tercile probabilities (0–100)
    below_normal_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    near_normal_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    above_normal_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Optional: percent of climatological normal (e.g. 80 = 20% below normal)
    precip_anomaly_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    region_label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Optional bounding box
    lat_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lat_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lon_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lon_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    uploaded_by_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    uploaded_by: Mapped[Optional[object]] = relationship("User", foreign_keys=[uploaded_by_id])
