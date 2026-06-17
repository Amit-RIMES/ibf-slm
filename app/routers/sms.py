"""Admin SMS / WhatsApp configuration."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.sms import _format_sms, _send_twilio, _send_africastalking, _send_webhook
from app.models.alert_recipient import AlertRecipient
from app.models.sms_config import SMSConfig

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_FORBIDDEN = HTMLResponse("Forbidden", status_code=403)


async def _get_or_create_config(db: AsyncSession) -> SMSConfig:
    cfg = await db.scalar(select(SMSConfig).where(SMSConfig.id == 1))
    if not cfg:
        cfg = SMSConfig(id=1)
        db.add(cfg)
        await db.commit()
        await db.refresh(cfg)
    return cfg


@router.get("/admin/sms", response_class=HTMLResponse)
async def sms_config_page(
    request: Request,
    saved: str = "",
    error: str = "",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    cfg = await _get_or_create_config(db)

    phone_count_r = await db.execute(
        select(AlertRecipient).where(
            AlertRecipient.is_active == True,  # noqa: E712
            AlertRecipient.phone != None,  # noqa: E711
        )
    )
    phone_recipients = phone_count_r.scalars().all()

    return templates.TemplateResponse(
        request,
        "sms_config.html",
        {
            "user": user,
            "cfg": cfg,
            "phone_recipients": phone_recipients,
            "saved": saved,
            "error": error,
        },
    )


@router.post("/admin/sms/config", response_class=HTMLResponse)
async def sms_config_save(
    request: Request,
    provider: str = Form("twilio"),
    enabled: str = Form(""),
    account_sid: str = Form(""),
    auth_token: str = Form(""),
    from_number: str = Form(""),
    whatsapp_enabled: str = Form(""),
    whatsapp_from: str = Form(""),
    webhook_url: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    cfg = await _get_or_create_config(db)
    cfg.provider = provider.strip()
    cfg.enabled = bool(enabled)
    cfg.account_sid = account_sid.strip() or None
    cfg.auth_token = auth_token.strip() or None
    cfg.from_number = from_number.strip() or None
    cfg.whatsapp_enabled = bool(whatsapp_enabled)
    cfg.whatsapp_from = whatsapp_from.strip() or None
    cfg.webhook_url = webhook_url.strip() or None
    await db.commit()

    return RedirectResponse("/admin/sms?saved=1", status_code=303)


@router.post("/admin/sms/test", response_class=HTMLResponse)
async def sms_test_send(
    request: Request,
    test_phone: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    cfg = await _get_or_create_config(db)
    if not cfg.enabled:
        return RedirectResponse("/admin/sms?error=SMS+not+enabled", status_code=303)

    phone = test_phone.strip()
    if not phone:
        return RedirectResponse("/admin/sms?error=Phone+number+required", status_code=303)

    body = "IBF App: SMS test message. Configuration is working correctly."
    cfg_dict = {
        "provider": cfg.provider,
        "enabled": cfg.enabled,
        "account_sid": cfg.account_sid,
        "auth_token": cfg.auth_token,
        "from_number": cfg.from_number,
        "whatsapp_enabled": cfg.whatsapp_enabled,
        "whatsapp_from": cfg.whatsapp_from,
        "webhook_url": cfg.webhook_url,
    }

    ok = False
    if cfg.provider == "twilio":
        ok = await _send_twilio(phone, body, cfg_dict)
    elif cfg.provider == "africastalking":
        ok = await _send_africastalking([phone], body, cfg_dict)
    elif cfg.provider == "webhook":
        ok = await _send_webhook([phone], body, cfg_dict)

    if ok:
        return RedirectResponse("/admin/sms?saved=test", status_code=303)
    return RedirectResponse("/admin/sms?error=Send+failed%2C+check+credentials+and+logs", status_code=303)
