import calendar
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.spi import TIMESCALES, spi_category
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.observed_rainfall import ObservedRainfall
from app.models.seasonal import SeasonalForecast
from app.models.spi import SPIRecord
from app.models.trigger import Trigger, TriggerActivation

router = APIRouter(prefix="/bulletin")
templates = Jinja2Templates(directory="app/templates")

_MONTH_ABBR = [calendar.month_abbr[i] for i in range(1, 13)]


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
        {"user": user, "spi_sources": spi_sources},
    )


# ── Generated bulletin ────────────────────────────────────────────────────────

@router.get("/generate", response_class=HTMLResponse)
async def bulletin_generate(
    request: Request,
    source: str = "CHIRPS",
    days: int = 30,
    title: str = "",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    cutoff_date = cutoff.date()

    # ── SPI status ────────────────────────────────────────────────────────────
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

    # ── Latest seasonal forecast ──────────────────────────────────────────────
    sf_r = await db.execute(
        select(SeasonalForecast)
        .order_by(SeasonalForecast.issue_date.desc())
        .limit(1)
    )
    latest_seasonal = sf_r.scalar_one_or_none()

    # ── Latest 15-day forecast ────────────────────────────────────────────────
    fc_r = await db.execute(
        select(ForecastUpload)
        .order_by(ForecastUpload.uploaded_at.desc())
        .limit(1)
    )
    latest_forecast = fc_r.scalar_one_or_none()

    # ── Active triggers ───────────────────────────────────────────────────────
    active_r = await db.execute(
        select(Trigger)
        .where(Trigger.is_active == True)  # noqa: E712
        .order_by(Trigger.hazard_type, Trigger.name)
    )
    active_triggers = active_r.scalars().all()

    # ── Recent activations in window ──────────────────────────────────────────
    act_r = await db.execute(
        select(TriggerActivation, Trigger)
        .join(Trigger, TriggerActivation.trigger_id == Trigger.id)
        .where(TriggerActivation.triggered_at >= cutoff)
        .order_by(TriggerActivation.triggered_at.desc())
        .limit(20)
    )
    activation_rows = [{"activation": act, "trigger": trig} for act, trig in act_r.all()]

    n_unacknowledged = sum(
        1 for r in activation_rows if r["activation"].status == "active"
    )

    # ── Recent impacts ────────────────────────────────────────────────────────
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

    # ── CHIRPS coverage ───────────────────────────────────────────────────────
    last_obs_r = await db.execute(
        select(ObservedRainfall).order_by(ObservedRainfall.obs_date.desc()).limit(1)
    )
    last_obs = last_obs_r.scalar_one_or_none()

    # ── Executive summary (derived text) ─────────────────────────────────────
    drought_status = "No active drought signal"
    worst_spi = None
    for ts in [6, 3, 1]:
        if ts in spi_current and spi_current[ts]["spi"] <= -1.0:
            worst_spi = spi_current[ts]
            break
    if worst_spi:
        drought_status = (
            f"SPI-{ts} indicates {worst_spi['label'].lower()} conditions "
            f"({worst_spi['spi']:+.2f}) as of "
            f"{worst_spi['month_name']} {worst_spi['year']}."
        )

    n_impacts = len(recent_impacts)
    impact_summary = (
        f"{n_impacts} impact event{'s' if n_impacts != 1 else ''} recorded in the past {days} days"
        + (f", affecting {total_affected:,} people" if total_affected else "")
        + "."
    )

    bulletin_title = title.strip() or f"IBF-SLM Situational Bulletin — {now.strftime('%B %Y')}"

    return templates.TemplateResponse(
        request, "bulletin.html",
        {
            "user": user,
            "now": now,
            "days": days,
            "source": source,
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
        },
    )
