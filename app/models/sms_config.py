from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SMSConfig(Base):
    __tablename__ = "sms_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    # "twilio" | "africastalking" | "webhook"
    provider: Mapped[str] = mapped_column(String(32), default="twilio")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Twilio: Account SID; Africa's Talking: username
    account_sid: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    # Twilio: Auth Token; Africa's Talking: API key
    auth_token: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    # E.164 sender number or shortcode
    from_number: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # WhatsApp-enabled Twilio number, e.g. "whatsapp:+14155238886"
    whatsapp_from: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # For "webhook" provider
    webhook_url: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
