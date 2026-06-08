from datetime import date, datetime, timezone
from typing import Optional
from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class ImpactRecord(Base):
    __tablename__ = "impact_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Event info
    event_name: Mapped[str] = mapped_column(String(255), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    hazard_type: Mapped[str] = mapped_column(String(64), nullable=False)  # flood, storm, drought, etc.

    # Location
    country: Mapped[str] = mapped_column(String(100), nullable=False)
    region: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Impact figures
    affected_population: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    casualties: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    displaced: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    damage_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Notes
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Optional link to a forecast
    forecast_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("forecast_uploads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    forecast: Mapped[Optional["ForecastUpload"]] = relationship("ForecastUpload", lazy="selectin")  # noqa: F821

    # Optional link to the trigger activation this impact validates
    trigger_activation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("trigger_activations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger_activation: Mapped[Optional["TriggerActivation"]] = relationship("TriggerActivation", lazy="selectin")  # noqa: F821
