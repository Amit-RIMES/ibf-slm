from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AlertRecipient(Base):
    __tablename__ = "alert_recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(256), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # SMS / WhatsApp — E.164 format, e.g. +6612345678
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
