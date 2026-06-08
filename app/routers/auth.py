import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_action
from app.core.config import settings
from app.core.database import get_db
from app.core.email import send_new_registration_email, send_password_reset_email
from app.core.rate_limit import forgot_password_limiter, login_limiter, register_limiter
from app.core.security import create_access_token, hash_password, verify_password
from app.models.reset_token import PasswordResetToken
from app.models.trigger import Trigger, TriggerSubscription
from app.models.user import User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _password_error(password: str) -> Optional[str]:
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if not any(c.isdigit() for c in password):
        return "Password must contain at least one digit."
    if not any(c.isalpha() for c in password):
        return "Password must contain at least one letter."
    return None


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html")


@router.post("/register")
async def register(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"
    if await register_limiter.is_limited(ip):
        return templates.TemplateResponse(
    request,
    "register.html",
    {"error": "Too many registration attempts. Please try again later."},
    status_code=429,
)

    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        return templates.TemplateResponse(
    request,
    "register.html",
    {"error": "Email already registered."},
    status_code=400,
)

    pw_err = _password_error(password)
    if pw_err:
        return templates.TemplateResponse(
    request,
    "register.html",
    {"error": pw_err},
    status_code=400,
)

    await register_limiter.record(ip)
    user = User(email=email, username=username, hashed_password=hash_password(password), is_active=False)
    db.add(user)
    await db.commit()

    import asyncio
    admins = await db.execute(select(User.email).where(User.role == "admin", User.is_active == True))  # noqa: E712
    admin_emails = [r[0] for r in admins.all()]
    asyncio.create_task(send_new_registration_email(admin_emails, username, email, settings.APP_BASE_URL))

    return RedirectResponse("/login?pending=1", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@router.post("/login")
async def login(
    response: Response,
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"

    limited, seconds = await login_limiter.is_limited(ip)
    if limited:
        minutes = max(1, round(seconds / 60))
        return templates.TemplateResponse(
    request,
    "login.html",
    {"error": f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}."},
    status_code=429,
)

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.hashed_password):
        await login_limiter.record_failure(ip)
        return templates.TemplateResponse(
    request,
    "login.html",
    {"error": "Invalid email or password."},
    status_code=401,
)

    if not user.is_active:
        return templates.TemplateResponse(
    request,
    "login.html",
    {"error": "Your account is pending admin approval."},
    status_code=403,
)

    await login_limiter.clear(ip)
    token = create_access_token({"sub": str(user.id)})
    redirect = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    redirect.set_cookie("access_token", token, httponly=True, samesite="lax")
    return redirect


@router.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("access_token")
    return response


@router.get("/account/profile", response_class=HTMLResponse)
async def edit_profile_page(request: Request, db: AsyncSession = Depends(get_db)):
    from app.core.deps import get_current_user
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
    request,
    "edit_profile.html",
    {"user": user, "username": user.username, "email": user.email},
)


@router.post("/account/profile", response_class=HTMLResponse)
async def edit_profile(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    from app.core.deps import get_current_user
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    def err(msg):
        return templates.TemplateResponse(
    request,
    "edit_profile.html",
    {"user": user, "username": username, "email": email, "error": msg},
)

    username = username.strip()
    email = email.strip().lower()

    if not username:
        return err("Username cannot be empty.")
    if not email:
        return err("Email cannot be empty.")

    if username != user.username:
        taken = await db.scalar(
            select(User).where(User.username == username, User.id != user.id)
        )
        if taken:
            return err("That username is already taken.")

    if email != user.email:
        taken = await db.scalar(
            select(User).where(User.email == email, User.id != user.id)
        )
        if taken:
            return err("That email address is already registered.")

    user.username = username
    user.email = email
    await db.commit()
    await log_action(db, user.id, "user.profile_edit", f"Updated profile: username='{username}', email='{email}'")

    return templates.TemplateResponse(
    request,
    "edit_profile.html",
    {"user": user, "username": username, "email": email, "success": True},
)


@router.get("/account/password", response_class=HTMLResponse)
async def change_password_page(request: Request, db: AsyncSession = Depends(get_db)):
    from app.core.deps import get_current_user
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "change_password.html", {"user": user})


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html")


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"
    if await forgot_password_limiter.is_limited(ip):
        return templates.TemplateResponse(
    request,
    "forgot_password.html",
    {"sent": True},
    # same neutral message to avoid info leak
            status_code=429,
)
    await forgot_password_limiter.record(ip)

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user:
        token_str = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(minutes=settings.RESET_TOKEN_EXPIRE_MINUTES)
        db.add(PasswordResetToken(token=token_str, user_id=user.id, expires_at=expires))
        await db.commit()
        reset_url = f"{settings.APP_BASE_URL}/reset-password/{token_str}"
        try:
            await send_password_reset_email(user.email, reset_url)
        except Exception:
            pass  # already logged in email utility

    # Always show the same message to avoid email enumeration
    return templates.TemplateResponse(
    request,
    "forgot_password.html",
    {"sent": True},
)


@router.get("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str, db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token == token,
            PasswordResetToken.used == False,  # noqa: E712
            PasswordResetToken.expires_at > now,
        )
    )
    reset_token = result.scalar_one_or_none()
    if not reset_token:
        return templates.TemplateResponse(
    request,
    "reset_password.html",
    {"invalid": True},
)
    return templates.TemplateResponse(
    request,
    "reset_password.html",
    {"token": token},
)


@router.post("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password(
    request: Request,
    token: str,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token == token,
            PasswordResetToken.used == False,  # noqa: E712
            PasswordResetToken.expires_at > now,
        )
    )
    reset_token = result.scalar_one_or_none()

    if not reset_token:
        return templates.TemplateResponse(
    request,
    "reset_password.html",
    {"invalid": True},
)

    def err(msg):
        return templates.TemplateResponse(
    request,
    "reset_password.html",
    {"token": token, "error": msg},
)

    pw_err = _password_error(new_password)
    if pw_err:
        return err(pw_err)
    if new_password != confirm_password:
        return err("Passwords do not match.")

    user_result = await db.execute(select(User).where(User.id == reset_token.user_id))
    user = user_result.scalar_one_or_none()
    if user:
        user.hashed_password = hash_password(new_password)
    reset_token.used = True
    await db.commit()

    return RedirectResponse("/login?reset=1", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/account/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request, db: AsyncSession = Depends(get_db)):
    from app.core.deps import get_current_user
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    triggers_result = await db.execute(
        select(Trigger).where(Trigger.is_active == True).order_by(Trigger.name)  # noqa: E712
    )
    active_triggers = triggers_result.scalars().all()

    subs_result = await db.execute(
        select(TriggerSubscription.trigger_id)
        .where(TriggerSubscription.user_id == user.id)
    )
    subscribed_ids = {row[0] for row in subs_result.all()}

    return templates.TemplateResponse(
    request,
    "account_notifications.html",
    {"user": user,
         "triggers": active_triggers, "subscribed_ids": subscribed_ids},
)


@router.post("/account/notifications", response_class=HTMLResponse)
async def save_notifications(
    request: Request,
    trigger_ids: Optional[List[int]] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    from app.core.deps import get_current_user
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    wanted_ids = set(trigger_ids) if trigger_ids else set()

    # Delete all existing subscriptions for this user
    existing_result = await db.execute(
        select(TriggerSubscription).where(TriggerSubscription.user_id == user.id)
    )
    for sub in existing_result.scalars().all():
        await db.delete(sub)

    # Verify the requested trigger IDs actually exist
    if wanted_ids:
        valid_result = await db.execute(
            select(Trigger.id).where(Trigger.id.in_(wanted_ids))
        )
        valid_ids = {row[0] for row in valid_result.all()}
    else:
        valid_ids = set()

    for tid in valid_ids:
        db.add(TriggerSubscription(user_id=user.id, trigger_id=tid))

    await db.commit()
    await log_action(db, user.id, "user.notifications_update",
                     f"Updated notification subscriptions: {len(valid_ids)} trigger(s)")

    triggers_result = await db.execute(
        select(Trigger).where(Trigger.is_active == True).order_by(Trigger.name)  # noqa: E712
    )
    active_triggers = triggers_result.scalars().all()

    return templates.TemplateResponse(
    request,
    "account_notifications.html",
    {"user": user,
         "triggers": active_triggers, "subscribed_ids": valid_ids, "success": True},
)


@router.post("/account/password", response_class=HTMLResponse)
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    from app.core.deps import get_current_user
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    def err(msg):
        return templates.TemplateResponse(
    request,
    "change_password.html",
    {"user": user, "error": msg},
)

    if not verify_password(current_password, user.hashed_password):
        return err("Current password is incorrect.")
    pw_err = _password_error(new_password)
    if pw_err:
        return err(pw_err)
    if new_password != confirm_password:
        return err("New passwords do not match.")

    user.hashed_password = hash_password(new_password)
    await db.commit()
    await log_action(db, user.id, "user.password_change", "Changed password")
    return templates.TemplateResponse(
    request,
    "change_password.html",
    {"user": user, "success": True},
)
