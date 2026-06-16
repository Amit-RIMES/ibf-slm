import logging
import logging.handlers
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.core.csrf import _token_for, validate_csrf
from app.core.database import Base, engine
from app.models import forecast, impact, trigger, sync, reset_token, audit, api_key, webhook, activation_comment, observed_rainfall, spi, seasonal, bulletin_schedule, risk_history, job_run, bulletin_draft, alert_recipient  # noqa: F401
from app.routers import admin, alerts, api, auth, bulletin, chat, dashboard, drought, forecasts, impacts, observed, reports, risk_overview as risk_overview_router, seasonal as seasonal_router, triggers, totp
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

# ── Prometheus metrics ─────────────────────────────────────────────────────────
try:
    from prometheus_fastapi_instrumentator import Instrumentator
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
except ImportError:
    pass  # optional dependency

# ── Security headers ───────────────────────────────────────────────────────────

_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://*.tile.openstreetmap.org; "
    "connect-src 'self'; "
    "frame-ancestors 'none';"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Only set CSP for HTML responses to avoid breaking SSE / binary streams
        ct = response.headers.get("content-type", "")
        if "text/html" in ct:
            response.headers["Content-Security-Policy"] = _CSP
        return response


app.add_middleware(SecurityHeadersMiddleware)


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
            if "application/x-www-form-urlencoded" in ct:
                try:
                    # Read via body() so Starlette caches the bytes in request._body.
                    # This lets the route handler re-read the form from the cache via
                    # stream() without hitting the "stream consumed" guard.
                    body_bytes = await request.body()
                    from urllib.parse import parse_qs
                    parsed = parse_qs(body_bytes.decode("utf-8", errors="replace"))
                    submitted = parsed.get("csrf_token", [""])[0]
                except Exception:
                    pass
            elif "multipart" in ct:
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
app.include_router(observed.router)
app.include_router(triggers.router)
app.include_router(sync_router.router)
app.include_router(totp.router)
app.include_router(chat.router)
app.include_router(drought.router)
app.include_router(reports.router)
app.include_router(seasonal_router.router)
app.include_router(bulletin.router)
app.include_router(risk_overview_router.router)


@app.get("/")
async def root():
    return RedirectResponse("/login")
