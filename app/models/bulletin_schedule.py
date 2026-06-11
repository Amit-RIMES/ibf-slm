from datetime import datetime, timezone

from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class BulletinSubscriber(Base):
    __tablename__ = "bulletin_subscribers"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    name: Mapped[str] = mapped_column(default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )


class BulletinSchedule(Base):
    """Single-row (id=1) bulletin delivery schedule."""

    __tablename__ = "bulletin_schedule"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    frequency: Mapped[str] = mapped_column(default="daily")   # "daily" | "weekly"
    day_of_week: Mapped[int] = mapped_column(default=0)        # 0=Mon … 6=Sun (weekly only)
    hour: Mapped[int] = mapped_column(default=7)               # UTC hour
    source: Mapped[str] = mapped_column(default="CHIRPS")
    days: Mapped[int] = mapped_column(default=30)              # bulletin window
    subject_template: Mapped[str] = mapped_column(
        default="IBF-SLM Bulletin — {month} {year}"
    )
    last_sent_at: Mapped[datetime | None] = mapped_column(nullable=True)
