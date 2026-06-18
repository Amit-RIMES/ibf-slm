import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_RETRY_DELAYS = [5, 10, 20]  # seconds between retries


async def _deliver(
    client: httpx.AsyncClient,
    url: str,
    body: str,
    headers: dict,
    webhook_id: int,
    activation_id: Optional[int],
) -> None:
    """Deliver to one webhook URL, retrying on 5xx. Logs every attempt to webhook_deliveries."""
    from app.core.database import SessionLocal
    from app.models.webhook_delivery import WebhookDelivery

    for attempt, delay in enumerate([0] + _RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        t0 = time.monotonic()
        status_code = None
        error = None
        success = False
        try:
            resp = await client.post(url, content=body, headers=headers, timeout=10)
            status_code = resp.status_code
            duration_ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code < 400:
                success = True
                async with SessionLocal() as db:
                    db.add(WebhookDelivery(
                        webhook_id=webhook_id, activation_id=activation_id, url=url,
                        status_code=status_code, attempt=attempt + 1,
                        success=True, duration_ms=duration_ms,
                        delivered_at=datetime.now(timezone.utc),
                    ))
                    await db.commit()
                return
            if 400 <= resp.status_code < 500:
                error = f"HTTP {resp.status_code} — not retrying"
                logger.warning("Webhook %s returned %d — not retrying", url, resp.status_code)
                break
            logger.warning(
                "Webhook %s returned %d (attempt %d/%d)",
                url, resp.status_code, attempt + 1, len(_RETRY_DELAYS) + 1,
            )
            error = f"HTTP {resp.status_code}"
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            error = str(exc)
            logger.warning(
                "Webhook delivery failed for %s (attempt %d/%d): %s",
                url, attempt + 1, len(_RETRY_DELAYS) + 1, exc,
            )

        # Log failed attempt
        async with SessionLocal() as db:
            db.add(WebhookDelivery(
                webhook_id=webhook_id, activation_id=activation_id, url=url,
                status_code=status_code, attempt=attempt + 1,
                success=False, error=error,
                duration_ms=int((time.monotonic() - t0) * 1000),
                delivered_at=datetime.now(timezone.utc),
            ))
            await db.commit()

    if not success:
        logger.error("Webhook %s failed after %d attempts — giving up", url, len(_RETRY_DELAYS) + 1)


async def send_webhook_notifications(fired_rows, webhooks) -> None:
    """POST to all active webhooks for each (trigger, activation, forecast) row."""
    if not webhooks or not fired_rows:
        return

    for trigger, activation, forecast in fired_rows:
        payload = {
            "event": "trigger.activation",
            "trigger": {
                "id": trigger.id,
                "name": trigger.name,
                "hazard_type": trigger.hazard_type,
                "variable": trigger.variable,
                "operator": trigger.operator,
                "threshold": trigger.threshold,
            },
            "activation": {
                "id": activation.id,
                "value": activation.value,
                "triggered_at": activation.triggered_at.astimezone(timezone.utc).isoformat(),
                "forecast_id": activation.forecast_id,
            },
            "forecast": {
                "id": forecast.id if forecast else None,
                "filename": forecast.filename if forecast else None,
                "source": forecast.source if forecast else None,
                "time_start": forecast.time_start if forecast else None,
                "time_end": forecast.time_end if forecast else None,
                "precip_mean": forecast.precip_mean if forecast else None,
                "precip_max": forecast.precip_max if forecast else None,
            },
        }
        body = json.dumps(payload, default=str)

        for wh in webhooks:
            headers = {"Content-Type": "application/json", "User-Agent": "IBF-App/1.0"}
            if wh.secret:
                sig = hmac.new(wh.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
                headers["X-IBF-Signature"] = f"sha256={sig}"
            async with httpx.AsyncClient() as client:
                await _deliver(client, wh.url, body, headers,
                               webhook_id=wh.id, activation_id=activation.id)
