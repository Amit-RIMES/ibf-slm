from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.core.database import Base, engine
from app.models import forecast  # noqa: F401 — registers ForecastUpload with Base
from app.routers import auth, dashboard, forecasts


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title="IBF App", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(forecasts.router)


@app.get("/")
async def root():
    return RedirectResponse("/login")
