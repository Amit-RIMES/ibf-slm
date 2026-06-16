from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CdsConfig(Base):
    """Singleton (id=1) storing Copernicus Data Store API credentials and sync settings."""
    __tablename__ = "cds_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    api_key: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    api_url: Mapped[str] = mapped_column(
        String(256), nullable=False,
        default="https://cds.climate.copernicus.eu/api/v2",
    )

    # Shared bounding box for all CDS datasets
    lat_min: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    lat_max: Mapped[float] = mapped_column(Float, default=35.0, nullable=False)
    lon_min: Mapped[float] = mapped_column(Float, default=60.0, nullable=False)
    lon_max: Mapped[float] = mapped_column(Float, default=155.0, nullable=False)

    # SEAS5 seasonal forecasts
    seas5_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    seas5_sync_hour: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    seas5_sync_minute: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    seas5_lead_months: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    seas5_last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    seas5_last_run_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    seas5_last_run_detail: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # ERA5 reanalysis backfill
    era5_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    era5_sync_hour: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    era5_sync_minute: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    era5_lookback_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    era5_last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    era5_last_run_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    era5_last_run_detail: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # GloFAS river discharge forecasts
    glofas_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    glofas_sync_hour: Mapped[int] = mapped_column(Integer, default=11, nullable=False)
    glofas_sync_minute: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    glofas_last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    glofas_last_run_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    glofas_last_run_detail: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
