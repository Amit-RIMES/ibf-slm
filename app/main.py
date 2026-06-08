import logging
import logging.handlers
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import settings
from app.core.csrf import _token_for, validate_csrf
from app.core.database import Base, engine
from app.models import forecast, impact, trigger, sync, reset_token, audit, api_key, webhook  # noqa: F401 — registers models with Base
from app.routers import admin, alerts, api, auth, dashboard, forecasts, impacts, triggers
from app.routers import sync as sync_router
from app.scheduler import apply_schedule, start_scheduler, stop_scheduler


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler = logging.handlers.RotatingFileHandler(
        settings.LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    start_scheduler()
    await apply_schedule()
    yield
    stop_scheduler()


app = FastAPI(title="IBF App", lifespan=lifespan)


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    jwt_token = request.cookies.get("access_token", "")
    request.state.csrf_token = _token_for(jwt_token) if jwt_token else ""

    if (
        request.method in ("POST", "PUT", "PATCH", "DELETE")
        and not request.url.path.startswith("/api/")
        and jwt_token
    ):
        submitted = request.headers.get("X-CSRF-Token", "")
        if not submitted:
            ct = request.headers.get("content-type", "")
            if "form" in ct or "multipart" in ct:
                try:
                    form = await request.form()
                    submitted = str(form.get("csrf_token", ""))
                except Exception:
                    pass
        if not validate_csrf(jwt_token, submitted):
            return HTMLResponse("<h1>403 — CSRF validation failed</h1>", status_code=403)

    return await call_next(request)


app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(alerts.router)
app.include_router(api.router)
app.include_router(dashboard.router)
app.include_router(forecasts.router)
app.include_router(impacts.router)
app.include_router(triggers.router)
app.include_router(sync_router.router)


@app.get("/")
async def root():
    return RedirectResponse("/login")
