from datetime import datetime, timezone

from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RiskScoreRecord(Base):
    __tablename__ = "risk_score_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    scored_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )
    source: Mapped[str] = mapped_column(default="CHIRPS")
    total: Mapped[int]
    level: Mapped[str]
    spi_pts: Mapped[int]
    seasonal_pts: Mapped[int]
    trigger_pts: Mapped[int]
    worst_spi: Mapped[float | None] = mapped_column(nullable=True)
