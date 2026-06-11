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
    var_label = {
        "precip_mean": "Mean precip", "precip_max": "Max precip", "precip_min": "Min precip",
        "spi_1": "SPI-1", "spi_3": "SPI-3", "spi_6": "SPI-6",
    }
    forecast = fired[0][2]
    source_label = forecast.filename if forecast else "SPI drought index"
    n = len(fired)
    rows = ""
    for trigger, activation, fc in fired:
        url = f"{base_url}/triggers/{trigger.id}"
        is_spi = trigger.variable.startswith("spi_")
        val_str = f"{activation.value:.3f}" + ("" if is_spi else " mm")
        thr_str = f"{op_sym[trigger.operator]} {trigger.threshold}" + ("" if is_spi else " mm")
        rows += f"""
        <tr>
          <td style="padding:.6rem .8rem;border-bottom:1px solid #f3f4f6;font-weight:600">{trigger.name}</td>
          <td style="padding:.6rem .8rem;border-bottom:1px solid #f3f4f6;text-transform:capitalize">{trigger.hazard_type}</td>
          <td style="padding:.6rem .8rem;border-bottom:1px solid #f3f4f6">{var_label.get(trigger.variable, trigger.variable)}</td>
          <td style="padding:.6rem .8rem;border-bottom:1px solid #f3f4f6;color:#dc2626;font-weight:700">{val_str}</td>
          <td style="padding:.6rem .8rem;border-bottom:1px solid #f3f4f6;color:#6b7280">{thr_str}</td>
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
          {n} trigger activation{'s' if n != 1 else ''} — {source_label}
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


async def send_acknowledgement_emails(
    emails: list[str],
    activation: "TriggerActivation",
    trigger: "Trigger",
    notes: str,
) -> None:
    """Notify subscribers that an alert they follow has been acknowledged."""
    if not emails:
        return
    if not settings.SMTP_HOST:
        logger.warning("SMTP not configured. Acknowledgement emails skipped.")
        return

    op_sym = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
    var_label = {"precip_mean": "Mean precip", "precip_max": "Max precip", "precip_min": "Min precip"}
    base_url = settings.APP_BASE_URL
    trigger_url = f"{base_url}/triggers/{trigger.id}"
    ack_time = activation.acknowledged_at.strftime("%d %b %Y, %H:%M UTC") if activation.acknowledged_at else "—"

    subject = f"[IBF Alert] Acknowledged — {trigger.name}"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:560px;margin:0 auto;padding:2rem;">
      <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;
                  padding:1rem 1.25rem;margin-bottom:1.5rem;">
        <span style="font-size:1.1rem">✓</span>
        <strong style="color:#0369a1;margin-left:.4rem;">
          Alert acknowledged — {trigger.name}
        </strong>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:.9rem;margin-bottom:1.25rem;">
        <tr>
          <td style="padding:.4rem 0;color:#6b7280;width:140px;">Trigger</td>
          <td style="font-weight:600;">{trigger.name}</td>
        </tr>
        <tr>
          <td style="padding:.4rem 0;color:#6b7280;">Rule</td>
          <td>{var_label.get(trigger.variable, trigger.variable)}
              {op_sym.get(trigger.operator, trigger.operator)} {trigger.threshold} mm</td>
        </tr>
        <tr>
          <td style="padding:.4rem 0;color:#6b7280;">Observed value</td>
          <td style="font-weight:700;">{activation.value:.3f} mm</td>
        </tr>
        <tr>
          <td style="padding:.4rem 0;color:#6b7280;">Acknowledged at</td>
          <td>{ack_time}</td>
        </tr>
        {"<tr><td style='padding:.4rem 0;color:#6b7280;'>Response notes</td>"
         f"<td style='font-style:italic;'>{notes}</td></tr>" if notes else ""}
      </table>
      <p>
        <a href="{trigger_url}"
           style="display:inline-block;padding:.65rem 1.25rem;background:#0369a1;
                  color:#fff;border-radius:8px;text-decoration:none;font-weight:600;">
          View trigger →
        </a>
      </p>
      <p style="color:#9ca3af;font-size:.8rem;margin-top:1.25rem;">
        You received this because you subscribed to alerts for this trigger.
        <a href="{base_url}/account/notifications" style="color:#6b7280;">Manage subscriptions</a>
      </p>
    </div>"""

    for email in emails:
        try:
            await asyncio.to_thread(_send_sync, email, subject, html)
            logger.info("Acknowledgement email sent to %s", email)
        except Exception as exc:
            logger.error("Failed to send acknowledgement email to %s: %s", email, exc)


async def send_sync_failure_email(admin_emails: list[str], n_consecutive: int, base_url: str) -> None:
    subject = f"[IBF] Daily sync has failed {n_consecutive} consecutive times"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;padding:2rem;">
      <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;
                  padding:1rem 1.25rem;margin-bottom:1.5rem;">
        <span style="font-size:1.1rem">⚠️</span>
        <strong style="color:#991b1b;margin-left:.4rem;">
          Daily sync failed {n_consecutive} consecutive time{'s' if n_consecutive != 1 else ''}
        </strong>
      </div>
      <p style="color:#4b5563;">
        The automatic daily forecast import has not succeeded for the last
        <strong>{n_consecutive}</strong> run{'s' if n_consecutive != 1 else ''}.
        Check the sync log and the RIMES portal for details.
      </p>
      <p style="margin-top:1.25rem;">
        <a href="{base_url}/admin/health"
           style="display:inline-block;padding:.65rem 1.25rem;background:#dc2626;
                  color:#fff;border-radius:8px;text-decoration:none;font-weight:600;">
          View system health →
        </a>
      </p>
      <p style="color:#9ca3af;font-size:.8rem;margin-top:1rem;">IBF App — {base_url}</p>
    </div>
    """
    if not settings.SMTP_HOST:
        logger.warning("SMTP not configured. Sync failure alert skipped (%d consecutive).", n_consecutive)
        return
    for email in admin_emails:
        try:
            await asyncio.to_thread(_send_sync, email, subject, html)
        except Exception as exc:
            logger.error("Failed to send sync failure alert to %s: %s", email, exc)


async def send_data_gap_email(admin_emails: list[str], gaps: dict, base_url: str) -> None:
    lines = []
    if gaps["chirps_alert"]:
        d = gaps["chirps_gap_days"]
        last = gaps["last_chirps_date"]
        lines.append(
            f"<li><strong>CHIRPS observed rainfall</strong>: no new data for <strong>{d} day{'s' if d != 1 else ''}</strong>"
            + (f" (last record: {last})" if last else "") + "</li>"
        )
    if gaps["forecast_alert"]:
        d = gaps["forecast_gap_days"]
        last = gaps["last_forecast_at"]
        lines.append(
            f"<li><strong>Forecast uploads</strong>: no new data for <strong>{d} day{'s' if d != 1 else ''}</strong>"
            + (f" (last upload: {last.strftime('%Y-%m-%d %H:%M UTC') if last else 'never'})" ) + "</li>"
        )
    if not lines:
        return
    subject = "[IBF] Data gap detected — missing input data"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;padding:2rem;">
      <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;
                  padding:1rem 1.25rem;margin-bottom:1.5rem;">
        <span style="font-size:1.1rem">⚠️</span>
        <strong style="color:#9a3412;margin-left:.4rem;">Data gap detected</strong>
      </div>
      <p style="color:#4b5563;">One or more data streams have not received new data:</p>
      <ul style="color:#374151;margin:1rem 0 1rem 1.25rem;line-height:1.8;">
        {''.join(lines)}
      </ul>
      <p style="color:#4b5563;">
        Stale data may affect trigger evaluations and SPI drought indices.
        Please check data sources and ingestion pipelines.
      </p>
      <p style="margin-top:1.25rem;">
        <a href="{base_url}/admin/health"
           style="display:inline-block;padding:.65rem 1.25rem;background:#ea580c;
                  color:#fff;border-radius:8px;text-decoration:none;font-weight:600;">
          View system health →
        </a>
      </p>
      <p style="color:#9ca3af;font-size:.8rem;margin-top:1rem;">IBF App — {base_url}</p>
    </div>
    """
    if not settings.SMTP_HOST:
        logger.warning("SMTP not configured. Data gap alert skipped.")
        return
    for email in admin_emails:
        try:
            await asyncio.to_thread(_send_sync, email, subject, html)
        except Exception as exc:
            logger.error("Failed to send data gap alert to %s: %s", email, exc)


async def send_escalation_email(
    admin_emails: list[str],
    activation: "TriggerActivation",
    trigger: "Trigger",
    hours_unacknowledged: int,
    base_url: str,
) -> None:
    """Re-notify admins that an activation has been unacknowledged for N hours."""
    if not settings.SMTP_HOST:
        logger.warning("SMTP not configured. Escalation email skipped for activation %d.", activation.id)
        return

    op_sym = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
    var_label = {"precip_mean": "Mean precip", "precip_max": "Max precip", "precip_min": "Min precip"}
    url = f"{base_url}/triggers/{trigger.id}"
    subject = f"[IBF ESCALATION] Unacknowledged alert — {trigger.name} ({hours_unacknowledged}h)"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:560px;margin:0 auto;padding:2rem;">
      <div style="background:#fff7ed;border:2px solid #fb923c;border-radius:8px;
                  padding:1rem 1.25rem;margin-bottom:1.5rem;">
        <span style="font-size:1.1rem">🔔</span>
        <strong style="color:#c2410c;margin-left:.4rem;">
          Escalation: alert unacknowledged for {hours_unacknowledged}+ hours
        </strong>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:.9rem;margin-bottom:1.25rem;">
        <tr>
          <td style="padding:.4rem 0;color:#6b7280;width:140px;">Trigger</td>
          <td style="font-weight:600;">{trigger.name}</td>
        </tr>
        <tr>
          <td style="padding:.4rem 0;color:#6b7280;">Rule</td>
          <td>{var_label.get(trigger.variable, trigger.variable)}
              {op_sym.get(trigger.operator, trigger.operator)} {trigger.threshold} mm</td>
        </tr>
        <tr>
          <td style="padding:.4rem 0;color:#6b7280;">Observed</td>
          <td style="font-weight:700;color:#dc2626;">{activation.value:.3f} mm</td>
        </tr>
        <tr>
          <td style="padding:.4rem 0;color:#6b7280;">Fired at</td>
          <td>{activation.triggered_at.strftime('%d %b %Y, %H:%M UTC')}</td>
        </tr>
      </table>
      <p>
        <a href="{url}"
           style="display:inline-block;padding:.65rem 1.25rem;background:#ea580c;
                  color:#fff;border-radius:8px;text-decoration:none;font-weight:600;">
          Acknowledge now →
        </a>
      </p>
      <p style="color:#9ca3af;font-size:.8rem;margin-top:1rem;">IBF App — {base_url}</p>
    </div>
    """

    for email in admin_emails:
        try:
            await asyncio.to_thread(_send_sync, email, subject, html)
            logger.info("Escalation email sent to %s for activation %d", email, activation.id)
        except Exception as exc:
            logger.error("Failed to send escalation email to %s: %s", email, exc)


async def send_weekly_digest_email(
    admin_emails: list[str],
    stats: dict,
    base_url: str,
) -> None:
    """Monday morning digest summarising the past week's activations, impacts, and coverage."""
    if not settings.SMTP_HOST:
        logger.warning("SMTP not configured. Weekly digest skipped.")
        return

    n_activations = stats.get("n_activations", 0)
    n_acknowledged = stats.get("n_acknowledged", 0)
    n_impacts = stats.get("n_impacts", 0)
    n_forecasts = stats.get("n_forecasts", 0)
    coverage_gaps = stats.get("coverage_gaps", [])
    top_hazards = stats.get("top_hazards", [])
    week_label = stats.get("week_label", "last week")

    hazard_rows = "".join(
        f"<tr><td style='padding:.35rem .6rem;text-transform:capitalize'>{h}</td>"
        f"<td style='padding:.35rem .6rem;font-weight:600;color:#4f46e5'>{c}</td></tr>"
        for h, c in top_hazards
    )
    gap_items = "".join(f"<li style='margin:.2rem 0'>{g}</li>" for g in coverage_gaps) or "<li>None</li>"

    subject = f"[IBF] Weekly digest — {week_label}"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:600px;margin:0 auto;padding:2rem;">
      <h2 style="color:#1a1a2e;margin-bottom:.25rem;">Weekly IBF Digest</h2>
      <p style="color:#6b7280;margin-top:0;">{week_label}</p>

      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:.75rem;margin:1.25rem 0;">
        <div style="background:#f0f9ff;border-radius:8px;padding:1rem 1.25rem;">
          <div style="font-size:1.8rem;font-weight:700;color:#0369a1">{n_activations}</div>
          <div style="color:#6b7280;font-size:.85rem">Trigger activations</div>
        </div>
        <div style="background:#f0fdf4;border-radius:8px;padding:1rem 1.25rem;">
          <div style="font-size:1.8rem;font-weight:700;color:#16a34a">{n_acknowledged}</div>
          <div style="color:#6b7280;font-size:.85rem">Acknowledged</div>
        </div>
        <div style="background:#fef9c3;border-radius:8px;padding:1rem 1.25rem;">
          <div style="font-size:1.8rem;font-weight:700;color:#ca8a04">{n_impacts}</div>
          <div style="color:#6b7280;font-size:.85rem">Impact records</div>
        </div>
        <div style="background:#faf5ff;border-radius:8px;padding:1rem 1.25rem;">
          <div style="font-size:1.8rem;font-weight:700;color:#7c3aed">{n_forecasts}</div>
          <div style="color:#6b7280;font-size:.85rem">Forecasts ingested</div>
        </div>
      </div>

      {'<h3 style="color:#374151;font-size:.95rem;">Activations by hazard type</h3><table style="width:100%;border-collapse:collapse;font-size:.9rem;margin-bottom:1.25rem;"><tbody>' + hazard_rows + '</tbody></table>' if top_hazards else ''}

      <h3 style="color:#374151;font-size:.95rem;">Coverage gaps (sources with no forecast this week)</h3>
      <ul style="margin:.5rem 0 1.25rem;padding-left:1.25rem;color:#4b5563;font-size:.9rem;">
        {gap_items}
      </ul>

      <p>
        <a href="{base_url}/dashboard"
           style="display:inline-block;padding:.65rem 1.25rem;background:#4f46e5;
                  color:#fff;border-radius:8px;text-decoration:none;font-weight:600;">
          Open dashboard →
        </a>
      </p>
      <p style="color:#9ca3af;font-size:.8rem;margin-top:1rem;">IBF App — {base_url}</p>
    </div>
    """

    for email in admin_emails:
        try:
            await asyncio.to_thread(_send_sync, email, subject, html)
            logger.info("Weekly digest sent to %s", email)
        except Exception as exc:
            logger.error("Failed to send weekly digest to %s: %s", email, exc)


async def send_trigger_activation_email(
    admin_emails: list[str],
    fired: list[tuple["Trigger", "TriggerActivation", "ForecastUpload"]],
) -> None:
    if not fired or not admin_emails:
        return

    n = len(fired)
    first_fc = fired[0][2]
    forecast_name = first_fc.filename if first_fc else "SPI drought index"
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
