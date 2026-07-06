from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CAPConfig(Base):
    """Singleton (id=1) — CAP publisher identity and inbound feed settings."""
    __tablename__ = "cap_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sender_id: Mapped[str] = mapped_column(String(255), nullable=False, default="ibf@example.int")
    sender_name: Mapped[str] = mapped_column(String(255), nullable=False, default="IBF Alert System")
    contact: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False, default="http://localhost:8000")
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="en-US")
    # Inbound CAP feed (Atom/RSS URL to poll for external CAP alerts)
    inbound_feed_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    inbound_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class CAPAlert(Base):
    """One CAP 1.2 alert message, optionally auto-generated from a TriggerActivation."""
    __tablename__ = "cap_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    identifier: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    sender: Mapped[str] = mapped_column(String(255), nullable=False)
    sent: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # CAP header
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="Actual")   # Actual/Test/Exercise
    msg_type: Mapped[str] = mapped_column(String(32), nullable=False, default="Alert")  # Alert/Update/Cancel
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="Public")

    # CAP <info> block
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="Met")
    event: Mapped[str] = mapped_column(String(255), nullable=False)
    urgency: Mapped[str] = mapped_column(String(32), nullable=False, default="Expected")
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="Moderate")
    certainty: Mapped[str] = mapped_column(String(32), nullable=False, default="Likely")
    onset: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    headline: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    instruction: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    web: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # CAP <area> block
    area_desc: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # CAP polygon: "lat,lon lat,lon ..." space-separated pairs
    polygon: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Link back to the trigger activation that generated this alert
    activation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("trigger_activations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # For Update/Cancel: "sender,identifier,sent" referencing the original Alert
    references: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
