from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SPIRecord(Base):
    __tablename__ = "spi_records"
    __table_args__ = (
        UniqueConstraint(
            "source", "year", "month", "timescale",
            name="uq_spi_source_year_month_scale",
        ),
        Index("ix_spi_source_timescale", "source", "timescale"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="CHIRPS")
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    timescale: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 3, or 6

    # Accumulated precipitation for this month (sum of daily precip_mean)
    monthly_precip_mm: Mapped[float] = mapped_column(Float, nullable=False)
    n_days: Mapped[int] = mapped_column(Integer, nullable=False)

    spi_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Number of same-calendar-month reference values used to fit the distribution
    n_reference: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
