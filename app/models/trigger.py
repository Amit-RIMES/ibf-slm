from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

VARIABLES = ["precip_mean", "precip_max", "precip_min"]
OPERATORS = ["gt", "gte", "lt", "lte"]
OPERATOR_SYMBOLS = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
OPERATOR_LABELS = {"gt": "greater than (>)", "gte": "at least (≥)", "lt": "less than (<)", "lte": "at most (≤)"}


class Trigger(Base):
    __tablename__ = "triggers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    hazard_type: Mapped[str] = mapped_column(String(64), nullable=False)
    variable: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(String(8), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Optional geographic scope (bounding box); None means whole-domain stats
    scope_lat_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scope_lat_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scope_lon_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scope_lon_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    activations: Mapped[list["TriggerActivation"]] = relationship(
        "TriggerActivation", back_populates="trigger", lazy="selectin",
        cascade="all, delete-orphan"
    )


class TriggerActivation(Base):
    __tablename__ = "trigger_activations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    trigger_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("triggers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    forecast_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("forecast_uploads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    value: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    trigger: Mapped["Trigger"] = relationship("Trigger", back_populates="activations", lazy="selectin")
    forecast: Mapped["ForecastUpload"] = relationship("ForecastUpload", lazy="selectin")  # noqa: F821
