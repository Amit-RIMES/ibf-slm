from datetime import datetime, timezone

from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(index=True)
    started_at: Mapped[datetime] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(default="ok")   # "ok" | "error" | "skipped"
    detail: Mapped[str] = mapped_column(default="")
