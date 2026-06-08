import hashlib
import hmac
import json
import logging
from datetime import timezone

import httpx

logger = logging.getLogger(__name__)


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
                "id": forecast.id,
                "filename": forecast.filename,
                "source": forecast.source,
                "time_start": forecast.time_start,
                "time_end": forecast.time_end,
                "precip_mean": forecast.precip_mean,
                "precip_max": forecast.precip_max,
            },
        }
        body = json.dumps(payload, default=str)

        for wh in webhooks:
            headers = {"Content-Type": "application/json", "User-Agent": "IBF-App/1.0"}
            if wh.secret:
                sig = hmac.new(wh.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
                headers["X-IBF-Signature"] = f"sha256={sig}"
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(wh.url, content=body, headers=headers, timeout=10)
            except Exception as exc:
                logger.warning("Webhook delivery failed for %s: %s", wh.url, exc)
