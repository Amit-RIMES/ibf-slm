from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.core.database import Base, engine
from app.routers import auth, dashboard


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title="IBF App", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(dashboard.router)


@app.get("/")
async def root():
    return RedirectResponse("/login")
