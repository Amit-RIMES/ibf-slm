"""SMS / WhatsApp alert sending via Twilio, Africa's Talking, or generic webhook."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_OP_SYM = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}


def _format_sms(fired_rows: list) -> str:
    """Build a compact SMS body (≤160 chars) from fired trigger rows."""
    n = len(fired_rows)
    if n == 0:
        return "IBF: trigger activation recorded."

    _, _, forecast = fired_rows[0]
    fc = forecast.filename if forecast else "N/A"

    if n == 1:
        trigger, activation, _ = fired_rows[0]
        op = _OP_SYM.get(trigger.operator, trigger.operator)
        msg = (
            f"⚡ IBF ALERT [{fc}]\n"
            f"{trigger.name}: {activation.value:.1f} {op} {trigger.threshold}"
        )
    else:
        hazards: dict[str, int] = {}
        for t, _, _ in fired_rows:
            hazards[t.hazard_type] = hazards.get(t.hazard_type, 0) + 1
        h_line = ", ".join(f"{h.title()}x{c}" for h, c in hazards.items())
        msg = f"⚡ IBF ALERT: {n} triggers [{fc}]\n{h_line}"

    return msg[:160]


async def _send_twilio(to: str, body: str, cfg: dict[str, Any]) -> bool:
    sid = cfg.get("account_sid") or ""
    token = cfg.get("auth_token") or ""
    from_num = cfg.get("from_number") or ""
    if not (sid and token and from_num):
        logger.warning("Twilio config incomplete — skipping to %s", to)
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                url, auth=(sid, token),
                data={"To": to, "From": from_num, "Body": body},
            )
        if r.status_code in (200, 201):
            logger.info("SMS sent via Twilio to %s", to)
            return True
        logger.warning("Twilio %s → %d: %s", to, r.status_code, r.text[:160])
        return False
    except Exception as exc:
        logger.error("Twilio error to %s: %s", to, exc)
        return False


async def _send_africastalking(phones: list[str], body: str, cfg: dict[str, Any]) -> bool:
    username = cfg.get("account_sid") or ""
    api_key = cfg.get("auth_token") or ""
    sender = cfg.get("from_number") or ""
    if not (username and api_key):
        logger.warning("Africa's Talking config incomplete — skipping batch")
        return False
    data: dict[str, str] = {
        "username": username,
        "to": ",".join(phones),
        "message": body,
    }
    if sender:
        data["from"] = sender
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.africastalking.com/version1/messaging",
                headers={"apiKey": api_key, "Accept": "application/json"},
                data=data,
            )
        if r.status_code == 201:
            logger.info("SMS sent via Africa's Talking to %d recipients", len(phones))
            return True
        logger.warning("Africa's Talking %d: %s", r.status_code, r.text[:160])
        return False
    except Exception as exc:
        logger.error("Africa's Talking error: %s", exc)
        return False


async def _send_webhook(phones: list[str], body: str, cfg: dict[str, Any]) -> bool:
    url = cfg.get("webhook_url") or ""
    if not url:
        logger.warning("SMS webhook URL not set — skipping")
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json={"to": phones, "message": body, "type": "sms"})
        ok = 200 <= r.status_code < 300
        if ok:
            logger.info("SMS sent via webhook to %d recipients", len(phones))
        else:
            logger.warning("SMS webhook %d: %s", r.status_code, r.text[:160])
        return ok
    except Exception as exc:
        logger.error("SMS webhook error: %s", exc)
        return False


async def send_trigger_activation_sms(
    phones_sms: list[str],
    phones_wa: list[str],
    fired_rows: list,
    cfg: dict[str, Any],
) -> None:
    """Send SMS and/or WhatsApp notifications for trigger activations."""
    if not cfg.get("enabled"):
        return

    body = _format_sms(fired_rows)
    provider = cfg.get("provider", "twilio")

    if phones_sms:
        if provider == "twilio":
            await asyncio.gather(*[_send_twilio(p, body, cfg) for p in phones_sms])
        elif provider == "africastalking":
            await _send_africastalking(phones_sms, body, cfg)
        elif provider == "webhook":
            await _send_webhook(phones_sms, body, cfg)

    if phones_wa and cfg.get("whatsapp_enabled") and cfg.get("whatsapp_from"):
        wa_cfg = {**cfg, "from_number": cfg["whatsapp_from"]}
        wa_tasks = [
            _send_twilio(
                p if p.startswith("whatsapp:") else f"whatsapp:{p}",
                body,
                wa_cfg,
            )
            for p in phones_wa
        ]
        await asyncio.gather(*wa_tasks)
