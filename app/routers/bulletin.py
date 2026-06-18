import calendar
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.i18n import (
    SUPPORTED_LANGUAGES,
    build_drought_status,
    build_impact_summary,
    get_translations,
)
from app.core.spi import TIMESCALES, spi_category
from app.models.bulletin_schedule import BulletinSchedule, BulletinSubscriber
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.observed_rainfall import ObservedRainfall
from app.models.seasonal import SeasonalForecast
from app.models.spi import SPIRecord
from app.models.trigger import Trigger, TriggerActivation

router = APIRouter(prefix="/bulletin")
templates = Jinja2Templates(directory="app/templates")

_MONTH_ABBR = [calendar.month_abbr[i] for i in range(1, 13)]
_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ── Shared context builder (used by route + scheduler) ───────────────────────

async def _build_bulletin_context(
    db: AsyncSession,
    source: str,
    days: int,
    title: str = "",
    username: str = "Scheduled",
    lang: str = "en",
) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    cutoff_date = cutoff.date()

    spi_r = await db.execute(
        select(SPIRecord)
        .where(SPIRecord.source == source)
        .order_by(SPIRecord.year, SPIRecord.month, SPIRecord.timescale)
    )
    spi_records = spi_r.scalars().all()

    by_scale: dict[int, list[SPIRecord]] = {ts: [] for ts in TIMESCALES}
    for rec in spi_records:
        if rec.timescale in by_scale:
            by_scale[rec.timescale].append(rec)

    spi_current: dict[int, dict] = {}
    for ts, recs in by_scale.items():
        latest = next((r for r in reversed(recs) if r.spi_value is not None), None)
        if latest:
            label, colour = spi_category(latest.spi_value)
            spi_current[ts] = {
                "spi": round(latest.spi_value, 2),
                "label": label,
                "colour": colour,
                "year": latest.year,
                "month": latest.month,
                "month_name": _MONTH_ABBR[latest.month - 1],
                "n_reference": latest.n_reference,
                "low_confidence": latest.n_reference < 5,
            }

    sf_r = await db.execute(
        select(SeasonalForecast).order_by(SeasonalForecast.issue_date.desc()).limit(1)
    )
    latest_seasonal = sf_r.scalar_one_or_none()

    fc_r = await db.execute(
        select(ForecastUpload).order_by(ForecastUpload.uploaded_at.desc()).limit(1)
    )
    latest_forecast = fc_r.scalar_one_or_none()

    active_r = await db.execute(
        select(Trigger)
        .where(Trigger.is_active == True)  # noqa: E712
        .order_by(Trigger.hazard_type, Trigger.name)
    )
    active_triggers = active_r.scalars().all()

    act_r = await db.execute(
        select(TriggerActivation, Trigger)
        .join(Trigger, TriggerActivation.trigger_id == Trigger.id)
        .where(TriggerActivation.triggered_at >= cutoff)
        .order_by(TriggerActivation.triggered_at.desc())
        .limit(20)
    )
    activation_rows = [{"activation": act, "trigger": trig} for act, trig in act_r.all()]
    n_unacknowledged = sum(1 for r in activation_rows if r["activation"].status == "active")

    imp_r = await db.execute(
        select(ImpactRecord)
        .where(ImpactRecord.event_date >= cutoff_date)
        .order_by(ImpactRecord.event_date.desc())
        .limit(15)
    )
    recent_impacts = imp_r.scalars().all()

    total_affected = sum(i.affected_population or 0 for i in recent_impacts)
    total_casualties = sum(i.casualties or 0 for i in recent_impacts)
    total_displaced = sum(i.displaced or 0 for i in recent_impacts)

    last_obs_r = await db.execute(
        select(ObservedRainfall).order_by(ObservedRainfall.obs_date.desc()).limit(1)
    )
    last_obs = last_obs_r.scalar_one_or_none()

    T = get_translations(lang)

    drought_status = T["drought_no_signal"]
    worst_spi = None
    worst_ts = None
    for ts in [6, 3, 1]:
        if ts in spi_current and spi_current[ts]["spi"] <= -1.0:
            worst_spi = spi_current[ts]
            worst_ts = ts
            break
    if worst_spi and worst_ts:
        drought_status = build_drought_status(
            T, worst_ts, worst_spi["spi"],
            worst_spi["label"], worst_spi["month_name"], worst_spi["year"],
        )

    n_impacts = len(recent_impacts)
    impact_summary = build_impact_summary(T, n_impacts, days, total_affected)

    days_window_str = T["days_window"].format(n=days)
    impacts_section_title = f"{T['impacts_section']} ({days_window_str})"
    no_impacts_msg = f"{T['no_impacts_prefix']} {days_window_str}."

    bulletin_title = title.strip() or f"IBF-SLM Situational Bulletin — {now.strftime('%B %Y')}"

    from types import SimpleNamespace
    fake_user = SimpleNamespace(username=username)

    return {
        "user": fake_user,
        "now": now,
        "days": days,
        "source": source,
        "lang": lang,
        "T": T,
        "bulletin_title": bulletin_title,
        "spi_current": spi_current,
        "latest_seasonal": latest_seasonal,
        "latest_forecast": latest_forecast,
        "active_triggers": active_triggers,
        "activation_rows": activation_rows,
        "n_unacknowledged": n_unacknowledged,
        "recent_impacts": recent_impacts,
        "total_affected": total_affected,
        "total_casualties": total_casualties,
        "total_displaced": total_displaced,
        "last_obs": last_obs,
        "drought_status": drought_status,
        "impact_summary": impact_summary,
        "impacts_section_title": impacts_section_title,
        "no_impacts_msg": no_impacts_msg,
        "supported_languages": SUPPORTED_LANGUAGES,
    }


def _render_bulletin_html(ctx: dict) -> str:
    """Render the bulletin template to a plain HTML string (no Request needed)."""
    tpl = templates.env.get_template("bulletin.html")
    return tpl.render(**ctx)


# ── Selection form ────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def bulletin_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    sources_r = await db.execute(select(SPIRecord.source).distinct())
    spi_sources = [r[0] for r in sources_r.all()] or ["CHIRPS"]

    return templates.TemplateResponse(
        request, "bulletin_form.html",
        {
            "user": user,
            "spi_sources": spi_sources,
            "supported_languages": SUPPORTED_LANGUAGES,
        },
    )


# ── Generated bulletin ────────────────────────────────────────────────────────

@router.get("/generate", response_class=HTMLResponse)
async def bulletin_generate(
    request: Request,
    source: str = "CHIRPS",
    days: int = 30,
    title: str = "",
    lang: str = "en",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    ctx = await _build_bulletin_context(db, source, days, title, username=user.username, lang=lang)
    ctx["user"] = user  # real user object for the route response
    return templates.TemplateResponse(request, "bulletin.html", ctx)


# ── Print / PDF export ────────────────────────────────────────────────────────

@router.get("/print", response_class=HTMLResponse)
async def bulletin_print(
    request: Request,
    source: str = "",
    days: int = 7,
    lang: str = "en",
    auto: int = 0,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    # Build the same context as bulletin_generate but return a print view
    ctx = await _build_bulletin_context(db, source or "CHIRPS", days, lang=lang)
    bulletin_html = _render_bulletin_html(ctx)

    # Wrap in a print-optimized page
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>IBF Bulletin — {now_str}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, Arial, sans-serif; background: #fff; color: #111; font-size: 12pt; }}

  .print-header {{
    display: flex; align-items: center; justify-content: space-between;
    border-bottom: 2px solid #4f46e5; padding: .75rem 1rem; margin-bottom: 1rem;
  }}
  .print-header .brand {{ font-size: 1.1rem; font-weight: 700; color: #4f46e5; }}
  .print-header .meta {{ font-size: .8rem; color: #6b7280; }}

  .no-print {{ display: block; background: #4f46e5; color: #fff; text-align: center;
    padding: .75rem; font-size: .9rem; font-weight: 600; cursor: pointer;
    border: none; width: 100%; margin-bottom: 1rem; }}

  @media print {{
    .no-print {{ display: none !important; }}
    body {{ margin: 0; }}
    @page {{ margin: 1.5cm; }}
  }}
</style>
</head>
<body>
<div class="print-header">
  <span class="brand">IBF Early Warning System — Bulletin</span>
  <span class="meta">Generated: {now_str} &nbsp;|&nbsp; Source: {source or "CHIRPS"} &nbsp;|&nbsp; Window: {days} days</span>
</div>
<button class="no-print" onclick="window.print()">🖨 Print / Save as PDF &nbsp;(then close this tab)</button>
{bulletin_html}
{('<script>window.onload=function(){window.print();}</script>' if auto else '')}
</body>
</html>"""
    return HTMLResponse(print_html)


# ── Schedule admin page ───────────────────────────────────────────────────────

async def _get_or_create_schedule(db: AsyncSession) -> BulletinSchedule:
    cfg = await db.scalar(select(BulletinSchedule).where(BulletinSchedule.id == 1))
    if not cfg:
        cfg = BulletinSchedule(id=1)
        db.add(cfg)
        await db.flush()
    return cfg


@router.get("/schedule", response_class=HTMLResponse)
async def bulletin_schedule_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/login", status_code=303)

    cfg = await _get_or_create_schedule(db)
    await db.commit()

    subscribers_r = await db.execute(
        select(BulletinSubscriber).order_by(BulletinSubscriber.created_at)
    )
    subscribers = subscribers_r.scalars().all()

    sources_r = await db.execute(select(SPIRecord.source).distinct())
    spi_sources = [r[0] for r in sources_r.all()] or ["CHIRPS"]

    return templates.TemplateResponse(
        request, "bulletin_schedule.html",
        {
            "user": user,
            "cfg": cfg,
            "subscribers": subscribers,
            "spi_sources": spi_sources,
            "day_names": _DAY_NAMES,
            "saved": request.query_params.get("saved"),
            "sent": request.query_params.get("sent"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/schedule", response_class=HTMLResponse)
async def bulletin_schedule_save(
    request: Request,
    enabled: str = Form(""),
    frequency: str = Form("daily"),
    day_of_week: int = Form(0),
    hour: int = Form(7),
    source: str = Form("CHIRPS"),
    days: int = Form(30),
    subject_template: str = Form("IBF-SLM Bulletin — {month} {year}"),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/login", status_code=303)

    cfg = await _get_or_create_schedule(db)
    cfg.enabled = bool(enabled)
    cfg.frequency = frequency if frequency in ("daily", "weekly") else "daily"
    cfg.day_of_week = max(0, min(6, day_of_week))
    cfg.hour = max(0, min(23, hour))
    cfg.source = source
    cfg.days = max(1, min(365, days))
    cfg.subject_template = subject_template or "IBF-SLM Bulletin — {month} {year}"
    await db.commit()

    from app.scheduler import apply_bulletin_schedule
    await apply_bulletin_schedule()

    return RedirectResponse("/bulletin/schedule?saved=1", status_code=303)


# ── Subscriber management ─────────────────────────────────────────────────────

@router.post("/subscribers/add", response_class=HTMLResponse)
async def subscriber_add(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/login", status_code=303)

    email = email.strip().lower()
    if "@" not in email:
        return RedirectResponse("/bulletin/schedule?error=invalid+email", status_code=303)

    existing = await db.scalar(
        select(BulletinSubscriber).where(BulletinSubscriber.email == email)
    )
    if not existing:
        db.add(BulletinSubscriber(email=email, name=name.strip()))
        await db.commit()

    return RedirectResponse("/bulletin/schedule?saved=1", status_code=303)


@router.post("/subscribers/{sub_id}/toggle", response_class=HTMLResponse)
async def subscriber_toggle(
    sub_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/login", status_code=303)

    sub = await db.scalar(select(BulletinSubscriber).where(BulletinSubscriber.id == sub_id))
    if sub:
        sub.is_active = not sub.is_active
        await db.commit()

    return RedirectResponse("/bulletin/schedule?saved=1", status_code=303)


@router.post("/subscribers/{sub_id}/delete", response_class=HTMLResponse)
async def subscriber_delete(
    sub_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/login", status_code=303)

    sub = await db.scalar(select(BulletinSubscriber).where(BulletinSubscriber.id == sub_id))
    if sub:
        await db.delete(sub)
        await db.commit()

    return RedirectResponse("/bulletin/schedule?saved=1", status_code=303)


# ── Manual send-now ───────────────────────────────────────────────────────────

@router.post("/schedule/send-now", response_class=HTMLResponse)
async def bulletin_send_now(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/login", status_code=303)

    cfg = await _get_or_create_schedule(db)
    subscribers_r = await db.execute(
        select(BulletinSubscriber).where(BulletinSubscriber.is_active == True)  # noqa: E712
    )
    recipients = [s.email for s in subscribers_r.scalars().all()]

    if not recipients:
        return RedirectResponse("/bulletin/schedule?error=no+recipients", status_code=303)

    ctx = await _build_bulletin_context(db, cfg.source, cfg.days, username=user.username)
    html = _render_bulletin_html(ctx)
    subject = _format_subject(cfg.subject_template, ctx["now"])

    from app.core.email import send_bulletin_email
    sent = await send_bulletin_email(recipients, subject, html)

    cfg.last_sent_at = datetime.now(timezone.utc)
    await db.commit()

    return RedirectResponse(f"/bulletin/schedule?sent={sent}", status_code=303)


def _format_subject(template: str, now: datetime) -> str:
    return template.format(month=now.strftime("%B"), year=now.strftime("%Y"))


# ── Bulletin drafts ───────────────────────────────────────────────────────────

async def _notify_admins(db: AsyncSession, subject: str, body: str) -> None:
    """Email all active admins a plain notification (fire-and-forget)."""
    from app.core.email import _send_sync
    from app.models.user import User
    import asyncio
    admins = (await db.execute(
        select(User).where(User.role == "admin").where(User.is_active == True)  # noqa: E712
    )).scalars().all()
    html = f"<p>{body}</p><p><a href='/bulletin/drafts'>View drafts →</a></p>"
    for admin in admins:
        if admin.email:
            await asyncio.to_thread(_send_sync, admin.email, subject, html)


async def _notify_user(db: AsyncSession, user_id: int, subject: str, body: str) -> None:
    """Email a specific user a plain notification (fire-and-forget)."""
    from app.core.email import _send_sync
    from app.models.user import User
    import asyncio
    u = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    if u and u.email:
        html = f"<p>{body}</p><p><a href='/bulletin/drafts'>View drafts →</a></p>"
        await asyncio.to_thread(_send_sync, u.email, subject, html)


@router.get("/drafts", response_class=HTMLResponse)
async def bulletin_drafts_list(request: Request, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import desc as _desc
    from app.models.bulletin_draft import BulletinDraft
    from app.models.user import User

    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    drafts_r = await db.execute(
        select(BulletinDraft)
        .order_by(_desc(BulletinDraft.created_at))
        .limit(100)
    )
    drafts = drafts_r.scalars().all()

    # Resolve user names for submitted_by / approved_by
    user_ids = {d.submitted_by_id for d in drafts if d.submitted_by_id} | \
               {d.approved_by_id for d in drafts if d.approved_by_id}
    users_by_id: dict = {}
    if user_ids:
        ur = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_by_id = {u.id: u.username for u in ur.scalars().all()}

    counts = {s: sum(1 for d in drafts if d.status == s)
              for s in ("pending", "submitted", "approved", "sent", "dismissed")}

    return templates.TemplateResponse(
        request, "bulletin_drafts.html",
        {
            "user": user,
            "drafts": drafts,
            "users_by_id": users_by_id,
            "counts": counts,
            "flash_submitted": request.query_params.get("submitted"),
            "flash_approved": request.query_params.get("approved"),
            "flash_rejected": request.query_params.get("rejected"),
            "flash_sent": request.query_params.get("sent"),
            "flash_error": request.query_params.get("error"),
        },
    )


@router.post("/drafts/{draft_id}/submit", response_class=HTMLResponse)
async def bulletin_draft_submit(
    draft_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from app.models.bulletin_draft import BulletinDraft
    from app.core.background import enqueue

    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    draft = await db.scalar(select(BulletinDraft).where(BulletinDraft.id == draft_id))
    if draft and draft.status == "pending":
        draft.status = "submitted"
        draft.submitted_by_id = user.id
        draft.submitted_at = datetime.now(timezone.utc)
        await db.commit()
        enqueue(_notify_admins(
            db,
            f"Bulletin #{draft_id} submitted for approval",
            f"{user.username} has submitted bulletin <strong>#{draft_id}: {draft.title or draft.source}</strong> "
            f"({draft.risk_level} risk) for your approval.",
        ))

    return RedirectResponse("/bulletin/drafts?submitted=1", status_code=303)


@router.post("/drafts/{draft_id}/approve", response_class=HTMLResponse)
async def bulletin_draft_approve(
    draft_id: int,
    request: Request,
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    from app.models.bulletin_draft import BulletinDraft
    from app.core.background import enqueue

    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/login", status_code=303)

    draft = await db.scalar(select(BulletinDraft).where(BulletinDraft.id == draft_id))
    if draft and draft.status == "submitted":
        draft.status = "approved"
        draft.approved_by_id = user.id
        draft.approved_at = datetime.now(timezone.utc)
        draft.approval_notes = notes.strip() or None
        await db.commit()
        if draft.submitted_by_id:
            msg = (f"Your bulletin <strong>#{draft_id}: {draft.title or draft.source}</strong> "
                   f"has been approved by {user.username} and is ready to send.")
            if notes:
                msg += f"<br>Note: {notes}"
            enqueue(_notify_user(db, draft.submitted_by_id,
                                 f"Bulletin #{draft_id} approved", msg))

    return RedirectResponse("/bulletin/drafts?approved=1", status_code=303)


@router.post("/drafts/{draft_id}/reject", response_class=HTMLResponse)
async def bulletin_draft_reject(
    draft_id: int,
    request: Request,
    notes: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    from app.models.bulletin_draft import BulletinDraft
    from app.core.background import enqueue

    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/login", status_code=303)

    draft = await db.scalar(select(BulletinDraft).where(BulletinDraft.id == draft_id))
    if draft and draft.status == "submitted":
        draft.status = "pending"  # return to draft for revision
        draft.approval_notes = notes.strip()
        await db.commit()
        if draft.submitted_by_id:
            enqueue(_notify_user(
                db, draft.submitted_by_id,
                f"Bulletin #{draft_id} returned for revision",
                f"Your bulletin <strong>#{draft_id}: {draft.title or draft.source}</strong> "
                f"has been returned for revision by {user.username}.<br>Reason: {notes}",
            ))

    return RedirectResponse("/bulletin/drafts?rejected=1", status_code=303)


@router.post("/drafts/{draft_id}/dismiss", response_class=HTMLResponse)
async def bulletin_draft_dismiss(
    draft_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from app.models.bulletin_draft import BulletinDraft

    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    draft = await db.scalar(select(BulletinDraft).where(BulletinDraft.id == draft_id))
    if draft and draft.status not in ("sent", "dismissed"):
        draft.status = "dismissed"
        await db.commit()

    return RedirectResponse("/bulletin/drafts", status_code=303)


@router.post("/drafts/{draft_id}/send", response_class=HTMLResponse)
async def bulletin_draft_send(
    draft_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from app.models.bulletin_draft import BulletinDraft

    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/login", status_code=303)

    draft = await db.scalar(select(BulletinDraft).where(BulletinDraft.id == draft_id))
    if not draft or draft.status not in ("approved", "pending"):
        return RedirectResponse("/bulletin/drafts", status_code=303)

    cfg_r = await db.execute(select(BulletinSchedule).where(BulletinSchedule.id == 1))
    cfg = cfg_r.scalar_one_or_none()
    subscribers_r = await db.execute(
        select(BulletinSubscriber).where(BulletinSubscriber.is_active == True)  # noqa: E712
    )
    recipients = [s.email for s in subscribers_r.scalars().all()]

    if not recipients:
        return RedirectResponse("/bulletin/drafts?error=no+recipients", status_code=303)

    days = cfg.days if cfg else 30
    ctx = await _build_bulletin_context(db, draft.source, days, title=draft.title, username=user.username)
    html = _render_bulletin_html(ctx)
    subject = draft.title or f"IBF-SLM {draft.risk_level} Risk Alert — {ctx['now'].strftime('%B %Y')}"

    from app.core.email import send_bulletin_email
    sent = await send_bulletin_email(recipients, subject, html)

    draft.status = "sent"
    draft.sent_at = datetime.now(timezone.utc)
    await db.commit()

    return RedirectResponse(f"/bulletin/drafts?sent={sent}", status_code=303)
