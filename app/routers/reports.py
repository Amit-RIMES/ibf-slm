import calendar
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.spi import TIMESCALES, spi_category
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.observed_rainfall import ObservedRainfall
from app.models.spi import SPIRecord
from app.models.trigger import Trigger, TriggerActivation

router = APIRouter(prefix="/reports")
templates = Jinja2Templates(directory="app/templates")

_MONTH_ABBR = [calendar.month_abbr[i] for i in range(1, 13)]

_VARIABLE_LABELS = {
    "precip_mean": "Mean precip",
    "precip_max": "Max precip",
    "precip_min": "Min precip",
    "spi_1": "SPI-1",
    "spi_3": "SPI-3",
    "spi_6": "SPI-6",
}


@router.get("/operational", response_class=HTMLResponse)
async def operational_report(
    request: Request,
    days: int = 30,
    source: str = "CHIRPS",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    cutoff_date = cutoff.date()

    # ── Current SPI status ────────────────────────────────────────────────────
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

    spi_n_months = len(by_scale[1])

    # ── Active triggers ───────────────────────────────────────────────────────
    active_r = await db.execute(
        select(Trigger)
        .where(Trigger.is_active == True)  # noqa: E712
        .order_by(Trigger.hazard_type, Trigger.name)
    )
    active_triggers = active_r.scalars().all()

    # ── Recent activations ────────────────────────────────────────────────────
    act_r = await db.execute(
        select(TriggerActivation, Trigger)
        .join(Trigger, TriggerActivation.trigger_id == Trigger.id)
        .where(TriggerActivation.triggered_at >= cutoff)
        .order_by(TriggerActivation.triggered_at.desc())
        .limit(50)
    )
    activation_rows = [
        {"activation": act, "trigger": trig}
        for act, trig in act_r.all()
    ]

    n_active_acts = sum(
        1 for r in activation_rows if r["activation"].status == "active"
    )

    # ── Latest forecast ───────────────────────────────────────────────────────
    fc_r = await db.execute(
        select(ForecastUpload)
        .order_by(ForecastUpload.uploaded_at.desc())
        .limit(1)
    )
    latest_forecast = fc_r.scalar_one_or_none()

    # ── Recent impacts ────────────────────────────────────────────────────────
    imp_r = await db.execute(
        select(ImpactRecord)
        .where(ImpactRecord.event_date >= cutoff_date)
        .order_by(ImpactRecord.event_date.desc())
        .limit(100)
    )
    recent_impacts = imp_r.scalars().all()

    total_affected = sum(i.affected_population or 0 for i in recent_impacts)
    total_casualties = sum(i.casualties or 0 for i in recent_impacts)

    # ── CHIRPS coverage ───────────────────────────────────────────────────────
    last_chirps_r = await db.execute(
        select(ObservedRainfall).order_by(ObservedRainfall.obs_date.desc()).limit(1)
    )
    last_chirps = last_chirps_r.scalar_one_or_none()

    chirps_total_r = await db.execute(
        select(func.count()).select_from(ObservedRainfall)
    )
    chirps_total = chirps_total_r.scalar_one() or 0

    return templates.TemplateResponse(
        request, "report_operational.html",
        {
            "user": user,
            "days": days,
            "source": source,
            "generated_at": now,
            "spi_current": spi_current,
            "spi_n_months": spi_n_months,
            "active_triggers": active_triggers,
            "activation_rows": activation_rows,
            "n_active_acts": n_active_acts,
            "latest_forecast": latest_forecast,
            "recent_impacts": recent_impacts,
            "total_affected": total_affected,
            "total_casualties": total_casualties,
            "last_chirps": last_chirps,
            "chirps_total": chirps_total,
            "variable_labels": _VARIABLE_LABELS,
        },
    )
