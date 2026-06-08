import pyotp
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_action
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.security import create_access_token

router = APIRouter(prefix="/account/2fa")
templates = Jinja2Templates(directory="app/templates")

_ISSUER = "IBF App"


@router.get("", response_class=HTMLResponse)
async def totp_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "totp_setup.html", {"user": user})


@router.post("/setup", response_class=HTMLResponse)
async def totp_setup(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    secret = pyotp.random_base32()
    user.totp_secret = secret
    await db.commit()

    otp_uri = pyotp.totp.TOTP(secret).provisioning_uri(user.email, issuer_name=_ISSUER)
    return templates.TemplateResponse(
        request,
        "totp_setup.html",
        {"user": user, "secret": secret, "otp_uri": otp_uri, "step": "confirm"},
    )


@router.post("/confirm", response_class=HTMLResponse)
async def totp_confirm(
    request: Request,
    code: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user or not user.totp_secret:
        return RedirectResponse("/account/2fa")

    totp = pyotp.TOTP(user.totp_secret)
    if not totp.verify(code.strip(), valid_window=1):
        secret = user.totp_secret
        otp_uri = totp.provisioning_uri(user.email, issuer_name=_ISSUER)
        return templates.TemplateResponse(
            request,
            "totp_setup.html",
            {"user": user, "secret": secret, "otp_uri": otp_uri,
             "step": "confirm", "error": "Invalid code — try again."},
            status_code=400,
        )

    user.totp_enabled = True
    await db.commit()
    await log_action(db, user.id, "user.totp_enabled", "Enabled two-factor authentication")
    return templates.TemplateResponse(
        request, "totp_setup.html", {"user": user, "step": "done"}
    )


@router.post("/disable", response_class=HTMLResponse)
async def totp_disable(
    request: Request,
    code: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user or not user.totp_enabled:
        return RedirectResponse("/account/2fa")

    totp = pyotp.TOTP(user.totp_secret)
    if not totp.verify(code.strip(), valid_window=1):
        return templates.TemplateResponse(
            request,
            "totp_setup.html",
            {"user": user, "step": "disable", "error": "Invalid code — 2FA not disabled."},
            status_code=400,
        )

    user.totp_enabled = False
    user.totp_secret = None
    await db.commit()
    await log_action(db, user.id, "user.totp_disabled", "Disabled two-factor authentication")
    return RedirectResponse("/account/2fa", status_code=303)


# ── TOTP verification step during login ───────────────────────────────────────

@router.get("/verify", response_class=HTMLResponse)
async def totp_verify_page(request: Request):
    pending = request.session.get("totp_pending_user_id") if hasattr(request, "session") else None
    if not pending:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "totp_verify.html", {})


@router.post("/verify", response_class=HTMLResponse)
async def totp_verify(
    request: Request,
    code: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    # We store the pending user id in a short-lived cookie (signed via JWT)
    pending_token = request.cookies.get("totp_pending")
    if not pending_token:
        return RedirectResponse("/login")

    from app.core.security import decode_access_token
    payload = decode_access_token(pending_token)
    if not payload:
        return RedirectResponse("/login")

    user_id = int(payload.get("sub", 0))
    from sqlalchemy import select
    from app.models.user import User
    user = await db.scalar(select(User).where(User.id == user_id))
    if not user or not user.totp_enabled:
        return RedirectResponse("/login")

    totp = pyotp.TOTP(user.totp_secret)
    if not totp.verify(code.strip(), valid_window=1):
        resp = templates.TemplateResponse(
            request, "totp_verify.html", {"error": "Invalid code — try again."}, status_code=401
        )
        return resp

    # Issue full session token
    token = create_access_token({"sub": str(user.id)})
    from fastapi.responses import RedirectResponse as RR
    redirect = RR("/dashboard", status_code=303)
    redirect.set_cookie("access_token", token, httponly=True, samesite="lax")
    redirect.delete_cookie("totp_pending")
    return redirect
