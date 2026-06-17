from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Station(Base):
    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    country: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    elevation_m: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class StationObservation(Base):
    __tablename__ = "station_observations"
    __table_args__ = (
        UniqueConstraint("station_id", "obs_date", name="uq_station_obs_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    obs_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Standard meteorological variables (all nullable — not every station measures everything)
    precip_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_max_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_min_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_mean_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_speed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    pressure_hpa: Mapped[float | None] = mapped_column(Float, nullable=True)

    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    is_provisional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
