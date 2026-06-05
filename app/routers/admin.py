from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import hashlib
import secrets

from app.core.audit import ACTION_LABELS, log_action
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.api_key import APIKey
from app.models.audit import AuditLog
from app.models.user import User

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
        "admin/users.html",
        {
            "request": request,
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


@router.get("/audit", response_class=HTMLResponse)
async def admin_audit(request: Request, db: AsyncSession = Depends(get_db), page: int = 1):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    page = max(1, page)
    total = await db.scalar(select(func.count()).select_from(AuditLog))
    total_pages = max(1, -(-total // AUDIT_PAGE_SIZE))
    page = min(page, total_pages)

    result = await db.execute(
        select(AuditLog)
        .order_by(desc(AuditLog.created_at))
        .offset((page - 1) * AUDIT_PAGE_SIZE)
        .limit(AUDIT_PAGE_SIZE)
    )
    entries = result.scalars().all()

    return templates.TemplateResponse(
        "admin/audit.html",
        {
            "request": request, "user": user, "entries": entries,
            "action_labels": ACTION_LABELS,
            "page": page, "total": total, "total_pages": total_pages,
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
        "admin/api_keys.html", {"request": request, "user": user, "keys": keys}
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
        "admin/api_keys.html",
        {"request": request, "user": user, "keys": keys, "new_key": raw_key, "new_key_name": name},
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
