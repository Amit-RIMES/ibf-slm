from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class BulletinDraft(Base):
    __tablename__ = "bulletin_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column()
    source: Mapped[str] = mapped_column(String(64))
    risk_level: Mapped[str] = mapped_column(String(32))
    total_score: Mapped[int] = mapped_column(default=0)
    title: Mapped[str] = mapped_column(String(256), default="")
    note: Mapped[str] = mapped_column(default="")
    # pending | submitted | approved | sent | dismissed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # workflow tracking
    submitted_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    submitted_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    approved_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    approved_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    approval_notes: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
