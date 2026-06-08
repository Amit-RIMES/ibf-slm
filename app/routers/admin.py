import hashlib
import os
import secrets
import sys

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import ACTION_CATEGORIES, ACTION_LABELS, log_action
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.api_key import APIKey
from app.models.audit import AuditLog
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.sync import SyncConfig, SyncLog
from app.models.trigger import Trigger, TriggerActivation
from app.models.user import User
from app.models.webhook import Webhook

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")

_FORBIDDEN = HTMLResponse(
    "<h1 style='font-family:system-ui;margin:3rem auto;max-width:400px'>403 — Admin access required</h1>",
    status_code=403,
)


@router.get("/users", response_class=HTMLResponse)
async def admin_users(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    pending_result = await db.execute(
        select(User).where(User.is_active == False).order_by(User.created_at)  # noqa: E712
    )
    active_result = await db.execute(
        select(User).where(User.is_active == True).order_by(User.id)  # noqa: E712
    )
    return templates.TemplateResponse(
    request,
    "admin/users.html",
    {
            "user": user,
            "pending_users": pending_result.scalars().all(),
            "users": active_result.scalars().all(),
        },
)


@router.post("/users/{target_id}/approve")
async def admin_approve_user(
    request: Request,
    target_id: int,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    result = await db.execute(select(User).where(User.id == target_id))
    target = result.scalar_one_or_none()
    if target:
        target.is_active = True
        await db.commit()
        await log_action(db, user.id, "user.approve", f"Approved registration for '{target.username}'")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{target_id}/role")
async def admin_change_role(
    request: Request,
    target_id: int,
    new_role: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN
    if target_id == user.id or new_role not in ("admin", "user"):
        return RedirectResponse("/admin/users", status_code=303)

    result = await db.execute(select(User).where(User.id == target_id))
    target = result.scalar_one_or_none()
    if target:
        target.role = new_role
        await db.commit()
        await log_action(db, user.id, "user.role_change", f"Changed '{target.username}' role to {new_role}")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{target_id}/scope")
async def admin_set_scope(
    request: Request,
    target_id: int,
    country_scope: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    result = await db.execute(select(User).where(User.id == target_id))
    target = result.scalar_one_or_none()
    if target:
        import json
        raw = country_scope.strip()
        if raw:
            # Accept comma-separated or JSON array
            try:
                parsed = json.loads(raw) if raw.startswith("[") else [s.strip() for s in raw.split(",") if s.strip()]
                target.country_scope = json.dumps(parsed) if parsed else None
            except Exception:
                target.country_scope = None
        else:
            target.country_scope = None
        await db.commit()
        await log_action(db, user.id, "user.scope_change",
                         f"Set country scope for '{target.username}': {target.country_scope or 'unrestricted'}")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{target_id}/delete")
async def admin_delete_user(
    request: Request,
    target_id: int,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN
    if target_id == user.id:
        return RedirectResponse("/admin/users", status_code=303)

    result = await db.execute(select(User).where(User.id == target_id))
    target = result.scalar_one_or_none()
    if target:
        uname = target.username
        was_pending = not target.is_active
        await db.delete(target)
        await db.commit()
        action = "user.reject" if was_pending else "user.delete"
        label = "Rejected registration" if was_pending else "Deleted user"
        await log_action(db, user.id, action, f"{label} '{uname}'")
    return RedirectResponse("/admin/users", status_code=303)


AUDIT_PAGE_SIZE = 50


def _audit_page_range(current: int, total_pages: int) -> list:
    if total_pages <= 7:
        return list(range(1, total_pages + 1))
    pages: list = []
    shown = sorted({1, total_pages, *range(max(1, current - 2), min(total_pages, current + 2) + 1)})
    prev = 0
    for p in shown:
        if p - prev > 1:
            pages.append(None)
        pages.append(p)
        prev = p
    return pages


@router.get("/audit", response_class=HTMLResponse)
async def admin_audit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    page: int = 1,
    user_q: str = "",
    category: str = "",
    date_from: str = "",
    date_to: str = "",
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    from datetime import datetime as dt, timezone as tz, timedelta
    from sqlalchemy import and_

    if category and category not in ACTION_CATEGORIES:
        category = ""

    dt_from = dt_to = None
    if date_from:
        try:
            dt_from = dt.fromisoformat(date_from).replace(tzinfo=tz.utc)
        except ValueError:
            date_from = ""
    if date_to:
        try:
            dt_to = (dt.fromisoformat(date_to) + timedelta(days=1)).replace(tzinfo=tz.utc)
        except ValueError:
            date_to = ""

    filters = []
    if category:
        filters.append(AuditLog.action.like(f"{category}.%"))
    if dt_from:
        filters.append(AuditLog.created_at >= dt_from)
    if dt_to:
        filters.append(AuditLog.created_at < dt_to)

    base = select(AuditLog)
    if user_q:
        base = base.join(User, User.id == AuditLog.user_id).where(
            User.username.ilike(f"%{user_q}%")
        )
    if filters:
        base = base.where(and_(*filters))

    page = max(1, page)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    total_pages = max(1, -(-total // AUDIT_PAGE_SIZE))
    page = min(page, total_pages)

    result = await db.execute(
        base.order_by(desc(AuditLog.created_at))
        .offset((page - 1) * AUDIT_PAGE_SIZE)
        .limit(AUDIT_PAGE_SIZE)
    )
    entries = result.scalars().all()

    return templates.TemplateResponse(
    request,
    "admin/audit.html",
    {
            "user": user, "entries": entries,
            "action_labels": ACTION_LABELS,
            "action_categories": ACTION_CATEGORIES,
            "page": page, "total": total, "total_pages": total_pages,
            "page_range": _audit_page_range(page, total_pages),
            "user_q": user_q, "category": category,
            "date_from": date_from, "date_to": date_to,
        },
)


@router.get("/api-keys", response_class=HTMLResponse)
async def admin_api_keys(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    result = await db.execute(select(APIKey).order_by(desc(APIKey.created_at)))
    keys = result.scalars().all()
    return templates.TemplateResponse(
    request,
    "admin/api_keys.html",
    {"user": user, "keys": keys},
)


@router.post("/api-keys/generate", response_class=HTMLResponse)
async def admin_generate_key(
    request: Request,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    raw_key = "ibf_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    prefix = raw_key[:12]

    api_key = APIKey(name=name.strip(), key_prefix=prefix, key_hash=key_hash, user_id=user.id)
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    result = await db.execute(select(APIKey).order_by(desc(APIKey.created_at)))
    keys = result.scalars().all()
    return templates.TemplateResponse(
    request,
    "admin/api_keys.html",
    {"user": user, "keys": keys, "new_key": raw_key, "new_key_name": name},
)


@router.post("/api-keys/{key_id}/revoke")
async def admin_revoke_key(request: Request, key_id: int, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    key = result.scalar_one_or_none()
    if key:
        key.is_active = False
        await db.commit()
    return RedirectResponse("/admin/api-keys", status_code=303)


@router.post("/api-keys/{key_id}/allowed-ips")
async def admin_set_allowed_ips(
    request: Request,
    key_id: int,
    allowed_ips: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    key = result.scalar_one_or_none()
    if key:
        key.allowed_ips = allowed_ips.strip() or None
        await db.commit()
    return RedirectResponse("/admin/api-keys", status_code=303)


@router.get("/webhooks", response_class=HTMLResponse)
async def admin_webhooks(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    result = await db.execute(select(Webhook).order_by(desc(Webhook.created_at)))
    webhooks = result.scalars().all()
    return templates.TemplateResponse(
    request,
    "admin/webhooks.html",
    {"user": user, "webhooks": webhooks},
)


@router.post("/webhooks/create")
async def admin_webhook_create(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    secret: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    wh = Webhook(name=name.strip(), url=url.strip(), secret=secret.strip() or None, is_active=True)
    db.add(wh)
    await db.commit()
    await log_action(db, user.id, "webhook.create", f"Created webhook '{name}'")
    return RedirectResponse("/admin/webhooks", status_code=303)


@router.post("/webhooks/{wh_id}/toggle")
async def admin_webhook_toggle(request: Request, wh_id: int, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    result = await db.execute(select(Webhook).where(Webhook.id == wh_id))
    wh = result.scalar_one_or_none()
    if wh:
        wh.is_active = not wh.is_active
        await db.commit()
        await log_action(db, user.id, "webhook.toggle",
                         f"{'Enabled' if wh.is_active else 'Disabled'} webhook '{wh.name}'")
    return RedirectResponse("/admin/webhooks", status_code=303)


@router.post("/webhooks/{wh_id}/delete")
async def admin_webhook_delete(request: Request, wh_id: int, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    result = await db.execute(select(Webhook).where(Webhook.id == wh_id))
    wh = result.scalar_one_or_none()
    if wh:
        wname = wh.name
        await db.delete(wh)
        await db.commit()
        await log_action(db, user.id, "webhook.delete", f"Deleted webhook '{wname}'")
    return RedirectResponse("/admin/webhooks", status_code=303)


@router.get("/health", response_class=HTMLResponse)
async def admin_health(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    from datetime import datetime, timezone

    # ── Row counts ────────────────────────────────────────────────
    async def count(model, *filters):
        stmt = select(func.count()).select_from(model)
        if filters:
            from sqlalchemy import and_
            stmt = stmt.where(and_(*filters))
        return await db.scalar(stmt)

    total_users      = await count(User)
    active_users     = await count(User, User.is_active == True)   # noqa: E712
    pending_users    = await count(User, User.is_active == False)  # noqa: E712
    total_forecasts  = await count(ForecastUpload)
    total_anomalies  = await count(ForecastUpload, ForecastUpload.is_anomaly == True)  # noqa: E712
    total_impacts    = await count(ImpactRecord)
    total_triggers   = await count(Trigger)
    active_triggers  = await count(Trigger, Trigger.is_active == True)   # noqa: E712
    total_acts       = await count(TriggerActivation)
    active_acts      = await count(TriggerActivation, TriggerActivation.status == "active")
    total_audit      = await count(AuditLog)
    active_keys      = await count(APIKey, APIKey.is_active == True)   # noqa: E712
    total_webhooks   = await count(Webhook)
    active_webhooks  = await count(Webhook, Webhook.is_active == True)  # noqa: E712
    total_sync_logs  = await count(SyncLog)

    # ── Sync config + recent logs ─────────────────────────────────
    sync_cfg = (await db.execute(
        select(SyncConfig).where(SyncConfig.id == 1)
    )).scalar_one_or_none()

    recent_sync_logs = (await db.execute(
        select(SyncLog).order_by(desc(SyncLog.run_at)).limit(20)
    )).scalars().all()

    # Sync log status counts
    sync_ok      = sum(1 for s in recent_sync_logs if s.status == "success")
    sync_skipped = sum(1 for s in recent_sync_logs if s.status == "skipped")
    sync_errors  = sum(1 for s in recent_sync_logs if s.status == "error")

    # ── DB file size ──────────────────────────────────────────────
    db_path_raw = settings.DATABASE_URL.split("///", 1)[-1]
    db_path_abs = os.path.abspath(db_path_raw)
    try:
        db_bytes = os.path.getsize(db_path_abs)
    except OSError:
        db_bytes = None

    def fmt_bytes(b):
        if b is None:
            return "—"
        for unit in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} GB"

    # ── SMTP ──────────────────────────────────────────────────────
    smtp = {
        "configured": bool(settings.SMTP_HOST),
        "host": settings.SMTP_HOST or "—",
        "port": settings.SMTP_PORT,
        "from_addr": settings.SMTP_FROM,
        "auth": bool(settings.SMTP_USER),
    }

    # ── System ────────────────────────────────────────────────────
    sys_info = {
        "python": sys.version.split()[0],
        "db_url": settings.DATABASE_URL,
        "db_path": db_path_abs,
        "db_size": fmt_bytes(db_bytes),
    }

    table_counts = [
        ("users",              total_users),
        ("forecast_uploads",   total_forecasts),
        ("impact_records",     total_impacts),
        ("triggers",           total_triggers),
        ("trigger_activations",total_acts),
        ("audit_logs",         total_audit),
        ("sync_log",           total_sync_logs),
        ("webhooks",           total_webhooks),
        ("api_keys",           active_keys),
    ]

    import json as _json
    sync_sources_count = 0
    if sync_cfg and sync_cfg.sources:
        try:
            sync_sources_count = len(_json.loads(sync_cfg.sources))
        except Exception:
            pass

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return templates.TemplateResponse(
    request,
    "admin/health.html",
    {
            "user": user,
            "now_utc": now_utc,
            # counts
            "total_users": total_users, "active_users": active_users, "pending_users": pending_users,
            "total_forecasts": total_forecasts, "total_anomalies": total_anomalies,
            "total_impacts": total_impacts,
            "total_triggers": total_triggers, "active_triggers": active_triggers,
            "total_acts": total_acts, "active_acts": active_acts,
            "total_audit": total_audit,
            "active_keys": active_keys,
            "total_webhooks": total_webhooks, "active_webhooks": active_webhooks,
            # sync
            "sync_cfg": sync_cfg,
            "recent_sync_logs": recent_sync_logs,
            "sync_ok": sync_ok, "sync_skipped": sync_skipped, "sync_errors": sync_errors,
            "sync_sources_count": sync_sources_count,
            "app_base_url": settings.APP_BASE_URL,
            # smtp + sys
            "smtp": smtp,
            "sys_info": sys_info,
            "table_counts": table_counts,
        },
)
