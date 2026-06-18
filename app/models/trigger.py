from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

FORECAST_VARIABLES = ["precip_mean", "precip_max", "precip_min"]
SPI_VARIABLES = ["spi_1", "spi_3", "spi_6"]
STATION_VARIABLES = ["station_precip_24h", "station_precip_48h"]
VARIABLES = FORECAST_VARIABLES + SPI_VARIABLES + STATION_VARIABLES
OPERATORS = ["gt", "gte", "lt", "lte"]
OPERATOR_SYMBOLS = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
OPERATOR_LABELS = {"gt": "greater than (>)", "gte": "at least (≥)", "lt": "less than (<)", "lte": "at most (≤)"}
LOGIC_OPS = ["and", "or"]


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

    # Optional second condition (AND/OR compound rule)
    condition_2_variable: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    condition_2_operator: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    condition_2_threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    logic_op: Mapped[str] = mapped_column(String(8), nullable=False, default="and", server_default="and")

    # Response plan / SOP attached to this trigger
    response_plan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Probabilistic mode: fire when P(variable > threshold) >= probability_threshold.
    # If null, the trigger uses deterministic evaluation.
    probability_threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Optional geographic scope: bounding box OR polygon (polygon takes precedence)
    scope_lat_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scope_lat_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scope_lon_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scope_lon_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scope_polygon: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # GeoJSON ring [[lon,lat],...]

    activations: Mapped[list["TriggerActivation"]] = relationship(
        "TriggerActivation", back_populates="trigger", lazy="selectin",
        cascade="all, delete-orphan"
    )


class TriggerSubscription(Base):
    __tablename__ = "trigger_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trigger_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("triggers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class TriggerActivation(Base):
    __tablename__ = "trigger_activations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    trigger_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("triggers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    forecast_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("forecast_uploads.id", ondelete="CASCADE"), nullable=True, index=True
    )
    value: Mapped[float] = mapped_column(Float, nullable=False)
    # For probabilistic triggers: the actual exceedance probability at activation time
    probability: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_escalated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Impact verification: did real impacts occur after this activation?
    impact_verdict: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # yes|partial|no
    impact_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    trigger: Mapped["Trigger"] = relationship("Trigger", back_populates="activations", lazy="selectin")
    forecast: Mapped["ForecastUpload"] = relationship("ForecastUpload", lazy="selectin")  # noqa: F821
