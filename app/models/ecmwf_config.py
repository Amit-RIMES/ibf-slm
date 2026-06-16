from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EcmwfConfig(Base):
    __tablename__ = "ecmwf_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1 (singleton)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    use_ensemble: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    run_time: Mapped[int] = mapped_column(Integer, default=0, nullable=False)   # 0/6/12/18 UTC
    sync_hour: Mapped[int] = mapped_column(Integer, default=10, nullable=False)  # when to run job
    sync_minute: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lat_min: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    lat_max: Mapped[float] = mapped_column(Float, default=35.0, nullable=False)
    lon_min: Mapped[float] = mapped_column(Float, default=60.0, nullable=False)
    lon_max: Mapped[float] = mapped_column(Float, default=155.0, nullable=False)
    # JSON array of ECMWF params to fetch, e.g. '["tp","2t"]'. Default: tp only.
    parameters: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, default='["tp"]')
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    last_run_detail: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
