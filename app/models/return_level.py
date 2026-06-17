from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReturnLevel(Base):
    __tablename__ = "return_levels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # "precip_mean" | "precip_max" | "precip_min"
    variable: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    n_years: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_obs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Return levels (mm) — None if not enough data
    rl_2: Mapped[float | None] = mapped_column(Float, nullable=True)
    rl_5: Mapped[float | None] = mapped_column(Float, nullable=True)
    rl_10: Mapped[float | None] = mapped_column(Float, nullable=True)
    rl_25: Mapped[float | None] = mapped_column(Float, nullable=True)
    rl_50: Mapped[float | None] = mapped_column(Float, nullable=True)
    rl_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    # GEV distribution parameters for reverse lookup (value → return period)
    gev_shape: Mapped[float | None] = mapped_column(Float, nullable=True)
    gev_loc: Mapped[float | None] = mapped_column(Float, nullable=True)
    gev_scale: Mapped[float | None] = mapped_column(Float, nullable=True)
