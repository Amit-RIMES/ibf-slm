import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:
    from app.models.forecast import ForecastUpload
    from app.models.trigger import Trigger, TriggerActivation

logger = logging.getLogger(__name__)


def _send_sync(to: str, subject: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        if settings.SMTP_USER:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.sendmail(settings.SMTP_FROM, to, msg.as_string())


async def send_password_reset_email(to: str, reset_url: str) -> None:
    subject = "IBF App — Password Reset"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;padding:2rem;">
      <h2 style="color:#1a1a2e">Reset your password</h2>
      <p style="color:#4b5563">Click the button below to set a new password.
         This link expires in {settings.RESET_TOKEN_EXPIRE_MINUTES} minutes.</p>
      <a href="{reset_url}"
         style="display:inline-block;margin:1.25rem 0;padding:.75rem 1.5rem;
                background:#4f46e5;color:#fff;border-radius:8px;
                text-decoration:none;font-weight:600;">
        Reset Password
      </a>
      <p style="color:#9ca3af;font-size:.8rem">
        If you didn't request this, ignore this email — your password won't change.
      </p>
    </div>
    """

    if not settings.SMTP_HOST:
        logger.warning("SMTP not configured. Password reset URL: %s", reset_url)
        return

    try:
        await asyncio.to_thread(_send_sync, to, subject, html)
    except Exception as exc:
        logger.error("Failed to send reset email to %s: %s", to, exc)
        raise


def _build_trigger_html(
    fired: list[tuple["Trigger", "TriggerActivation", "ForecastUpload"]],
    base_url: str,
) -> str:
    op_sym = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
    var_label = {"precip_mean": "Mean precip", "precip_max": "Max precip", "precip_min": "Min precip"}
    forecast = fired[0][2]
    n = len(fired)
    rows = ""
    for trigger, activation, fc in fired:
        url = f"{base_url}/triggers/{trigger.id}"
        rows += f"""
        <tr>
          <td style="padding:.6rem .8rem;border-bottom:1px solid #f3f4f6;font-weight:600">{trigger.name}</td>
          <td style="padding:.6rem .8rem;border-bottom:1px solid #f3f4f6;text-transform:capitalize">{trigger.hazard_type}</td>
          <td style="padding:.6rem .8rem;border-bottom:1px solid #f3f4f6">{var_label.get(trigger.variable, trigger.variable)}</td>
          <td style="padding:.6rem .8rem;border-bottom:1px solid #f3f4f6;color:#dc2626;font-weight:700">{activation.value:.3f} mm</td>
          <td style="padding:.6rem .8rem;border-bottom:1px solid #f3f4f6;color:#6b7280">{op_sym[trigger.operator]} {trigger.threshold} mm</td>
          <td style="padding:.6rem .8rem;border-bottom:1px solid #f3f4f6">
            <a href="{url}" style="color:#4f46e5;font-weight:600">View →</a>
          </td>
        </tr>"""

    return f"""
    <div style="font-family:system-ui,sans-serif;max-width:680px;margin:0 auto;padding:2rem;">
      <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;
                  padding:1rem 1.25rem;margin-bottom:1.5rem;">
        <span style="font-size:1.1rem">⚠️</span>
        <strong style="color:#991b1b;margin-left:.4rem;">
          {n} trigger activation{'s' if n != 1 else ''} — {forecast.filename}
        </strong>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:.9rem;">
        <thead>
          <tr style="font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;color:#9ca3af;">
            <th style="padding:.5rem .8rem;text-align:left;border-bottom:2px solid #e5e7eb">Trigger</th>
            <th style="padding:.5rem .8rem;text-align:left;border-bottom:2px solid #e5e7eb">Hazard</th>
            <th style="padding:.5rem .8rem;text-align:left;border-bottom:2px solid #e5e7eb">Variable</th>
            <th style="padding:.5rem .8rem;text-align:left;border-bottom:2px solid #e5e7eb">Observed</th>
            <th style="padding:.5rem .8rem;text-align:left;border-bottom:2px solid #e5e7eb">Threshold</th>
            <th style="padding:.5rem .8rem;text-align:left;border-bottom:2px solid #e5e7eb"></th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="margin-top:1.5rem;">
        <a href="{base_url}/triggers"
           style="display:inline-block;padding:.65rem 1.25rem;background:#4f46e5;
                  color:#fff;border-radius:8px;text-decoration:none;font-weight:600;">
          View all triggers
        </a>
      </p>
      <p style="color:#9ca3af;font-size:.8rem;margin-top:1rem;">
        IBF App — {base_url}
      </p>
    </div>"""


async def send_new_registration_email(admin_emails: list[str], username: str, email: str, base_url: str) -> None:
    subject = f"[IBF] New registration pending approval — {username}"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;padding:2rem;">
      <h2 style="color:#1a1a2e">New registration pending approval</h2>
      <p style="color:#4b5563">A new user has registered and is waiting for your approval.</p>
      <table style="width:100%;border-collapse:collapse;font-size:.9rem;margin:1rem 0;">
        <tr><td style="padding:.4rem 0;color:#6b7280;width:80px">Username</td><td style="font-weight:600">{username}</td></tr>
        <tr><td style="padding:.4rem 0;color:#6b7280">Email</td><td>{email}</td></tr>
      </table>
      <a href="{base_url}/admin/users"
         style="display:inline-block;margin-top:.5rem;padding:.65rem 1.25rem;
                background:#4f46e5;color:#fff;border-radius:8px;
                text-decoration:none;font-weight:600;">
        Review in Admin Panel
      </a>
    </div>
    """

    if not settings.SMTP_HOST:
        logger.warning("SMTP not configured. New registration pending: %s <%s>", username, email)
        return

    for admin_email in admin_emails:
        try:
            await asyncio.to_thread(_send_sync, admin_email, subject, html)
        except Exception as exc:
            logger.error("Failed to send registration alert to %s: %s", admin_email, exc)


async def send_subscriber_alert_emails(
    fired: list[tuple["Trigger", "TriggerActivation", "ForecastUpload"]],
    email_to_trigger_ids: dict[str, set[int]],
) -> None:
    """Send each subscriber an email for only the triggers they opted into."""
    if not fired:
        return
    if not settings.SMTP_HOST:
        logger.warning("SMTP not configured. Subscriber alerts skipped.")
        return

    trigger_map = {trig.id: (trig, act, fc) for trig, act, fc in fired}
    for email, subscribed_ids in email_to_trigger_ids.items():
        subscriber_fired = [trigger_map[tid] for tid in subscribed_ids if tid in trigger_map]
        if not subscriber_fired:
            continue
        n = len(subscriber_fired)
        forecast_name = subscriber_fired[0][2].filename
        subject = f"[IBF Alert] {n} trigger activation{'s' if n != 1 else ''} — {forecast_name}"
        html = _build_trigger_html(subscriber_fired, settings.APP_BASE_URL)
        try:
            await asyncio.to_thread(_send_sync, email, subject, html)
            logger.info("Subscriber alert sent to %s", email)
        except Exception as exc:
            logger.error("Failed to send subscriber alert to %s: %s", email, exc)


async def send_trigger_activation_email(
    admin_emails: list[str],
    fired: list[tuple["Trigger", "TriggerActivation", "ForecastUpload"]],
) -> None:
    if not fired or not admin_emails:
        return

    n = len(fired)
    forecast_name = fired[0][2].filename
    subject = f"[IBF Alert] {n} trigger activation{'s' if n != 1 else ''} — {forecast_name}"
    html = _build_trigger_html(fired, settings.APP_BASE_URL)

    if not settings.SMTP_HOST:
        logger.warning(
            "SMTP not configured. Trigger alert skipped. %d activation(s) for %s",
            n, forecast_name,
        )
        return

    for email in admin_emails:
        try:
            await asyncio.to_thread(_send_sync, email, subject, html)
            logger.info("Trigger alert sent to %s", email)
        except Exception as exc:
            logger.error("Failed to send trigger alert to %s: %s", email, exc)
