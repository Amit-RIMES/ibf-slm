from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.core.database import Base, engine
from app.models import forecast, impact, trigger  # noqa: F401 — registers models with Base
from app.routers import auth, dashboard, forecasts, impacts, triggers


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title="IBF App", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(forecasts.router)
app.include_router(impacts.router)
app.include_router(triggers.router)


@app.get("/")
async def root():
    return RedirectResponse("/login")
