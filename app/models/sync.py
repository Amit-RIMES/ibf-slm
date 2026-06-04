from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class SyncConfig(Base):
    __tablename__ = "sync_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1 (singleton)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sources: Mapped[str] = mapped_column(Text, default="[]", nullable=False)  # JSON list of source keys
    sync_hour: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    sync_minute: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # ok / error / partial


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    date: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # success / skipped / error
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    forecast_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
