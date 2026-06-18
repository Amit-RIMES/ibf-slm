from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    webhook_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("webhooks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    activation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    success: Mapped[bool] = mapped_column(Integer, nullable=False, default=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
