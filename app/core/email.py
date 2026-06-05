import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

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
