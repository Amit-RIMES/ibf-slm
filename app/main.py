from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.core.database import Base, engine
from app.models import forecast, impact, trigger, sync, reset_token  # noqa: F401 — registers models with Base
from app.routers import admin, auth, dashboard, forecasts, impacts, triggers
from app.routers import sync as sync_router
from app.scheduler import apply_schedule, start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    start_scheduler()
    await apply_schedule()
    yield
    stop_scheduler()


app = FastAPI(title="IBF App", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(dashboard.router)
app.include_router(forecasts.router)
app.include_router(impacts.router)
app.include_router(triggers.router)
app.include_router(sync_router.router)


@app.get("/")
async def root():
    return RedirectResponse("/login")
