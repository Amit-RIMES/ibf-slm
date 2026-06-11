import calendar
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.background import enqueue
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.risk import compute_risk_score
from app.core.spi import TIMESCALES, spi_category
from app.models.seasonal import SeasonalForecast
from app.models.spi import SPIRecord
from app.models.trigger import TriggerActivation

router = APIRouter(prefix="/drought")
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

_FORBIDDEN = HTMLResponse(
    "<h1 style='font-family:system-ui;margin:3rem auto;max-width:400px'>403 — Admin access required</h1>",
    status_code=403,
)

_MONTH_ABBR = [calendar.month_abbr[i] for i in range(1, 13)]


@router.get("", response_class=HTMLResponse)
async def drought_dashboard(
    request: Request,
    source: str = "CHIRPS",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    # All records for this source, ordered chronologically
    result = await db.execute(
        select(SPIRecord)
        .where(SPIRecord.source == source)
        .order_by(SPIRecord.year, SPIRecord.month, SPIRecord.timescale)
    )
    records = result.scalars().all()

    # Distinct sources for the selector
    sources_r = await db.execute(
        select(SPIRecord.source).distinct()
    )
    sources = [r[0] for r in sources_r.all()] or ["CHIRPS"]

    # Latest seasonal forecast
    sf_r = await db.execute(
        select(SeasonalForecast).order_by(SeasonalForecast.issue_date.desc()).limit(1)
    )
    latest_seasonal = sf_r.scalar_one_or_none()

    # Active trigger count for risk score
    n_active = await db.scalar(
        select(func.count()).select_from(TriggerActivation)
        .where(TriggerActivation.status == "active")
    )

    if not records:
        return templates.TemplateResponse(
            request, "drought_dashboard.html",
            {
                "user": user,
                "source": source,
                "sources": sources,
                "current": {},
                "chart_labels": [],
                "chart_spi1": [],
                "chart_spi3": [],
                "chart_spi6": [],
                "drought_events": [],
                "n_months": 0,
                "baseline_info": None,
                "heatmap_years": [],
                "heatmap_json": "{}",
                "latest_seasonal": latest_seasonal,
                "risk": compute_risk_score({}, latest_seasonal, n_active or 0),
            },
        )

    # Group by timescale → (year, month) → spi_value
    by_scale: dict[int, list[SPIRecord]] = {ts: [] for ts in TIMESCALES}
    for rec in records:
        if rec.timescale in by_scale:
            by_scale[rec.timescale].append(rec)

    # Current status: latest record per timescale that has a non-None SPI
    current: dict[int, dict] = {}
    for ts, recs in by_scale.items():
        latest = next(
            (r for r in reversed(recs) if r.spi_value is not None), None
        )
        if latest:
            label, colour = spi_category(latest.spi_value)
            current[ts] = {
                "spi": round(latest.spi_value, 2),
                "label": label,
                "colour": colour,
                "year": latest.year,
                "month": latest.month,
                "month_name": _MONTH_ABBR[latest.month - 1],
                "precip_mm": round(latest.monthly_precip_mm, 1),
                "n_days": latest.n_days,
                "n_reference": latest.n_reference,
                "low_confidence": latest.n_reference < 5,
            }

    # Chart data: use SPI-1 records as the time axis; last 36 months
    spi1_recs = by_scale[1][-36:]
    chart_labels = [
        f"{_MONTH_ABBR[r.month - 1]} {r.year}" for r in spi1_recs
    ]

    def _chart_vals(ts: int, keys: list[tuple[int, int]]) -> list[float | None]:
        lookup = {(r.year, r.month): r.spi_value for r in by_scale[ts]}
        return [
            (round(lookup[k], 2) if lookup.get(k) is not None else None)
            for k in keys
        ]

    keys = [(r.year, r.month) for r in spi1_recs]
    chart_spi1 = _chart_vals(1, keys)
    chart_spi3 = _chart_vals(3, keys)
    chart_spi6 = _chart_vals(6, keys)

    # Heatmap data: year × month grid per timescale
    heatmap_years = sorted({r.year for r in records})
    heatmap: dict[int, dict] = {}
    for ts, recs in by_scale.items():
        lookup = {(r.year, r.month): r for r in recs}
        heatmap[ts] = {}
        for yr in heatmap_years:
            heatmap[ts][yr] = {}
            for mo in range(1, 13):
                rec = lookup.get((yr, mo))
                if rec and rec.spi_value is not None:
                    _, colour = spi_category(rec.spi_value)
                    heatmap[ts][yr][mo] = {
                        "v": round(rec.spi_value, 2),
                        "c": colour,
                        "n": rec.n_reference,
                    }
                else:
                    heatmap[ts][yr][mo] = None

    # Drought events: months where any timescale SPI <= -1
    drought_set: dict[tuple[int, int], dict] = {}
    for ts, recs in by_scale.items():
        for rec in recs:
            if rec.spi_value is not None and rec.spi_value <= -1.0:
                key = (rec.year, rec.month)
                if key not in drought_set:
                    drought_set[key] = {
                        "year": rec.year,
                        "month": rec.month,
                        "month_name": _MONTH_ABBR[rec.month - 1],
                        "spi1": None, "spi3": None, "spi6": None,
                    }
                drought_set[key][f"spi{ts}"] = round(rec.spi_value, 2)

    drought_events = sorted(drought_set.values(), key=lambda d: (d["year"], d["month"]), reverse=True)[:24]

    # Baseline info
    all_spi1 = by_scale[1]
    if all_spi1:
        first = all_spi1[0]
        last = all_spi1[-1]
        baseline_info = (
            f"{_MONTH_ABBR[first.month - 1]} {first.year} – "
            f"{_MONTH_ABBR[last.month - 1]} {last.year} "
            f"({len(all_spi1)} months)"
        )
    else:
        baseline_info = None

    return templates.TemplateResponse(
        request, "drought_dashboard.html",
        {
            "user": user,
            "heatmap_years": heatmap_years,
            "heatmap_json": json.dumps(heatmap),
            "source": source,
            "sources": sources,
            "current": current,
            "chart_labels": chart_labels,
            "chart_spi1": chart_spi1,
            "chart_spi3": chart_spi3,
            "chart_spi6": chart_spi6,
            "drought_events": drought_events,
            "n_months": len(by_scale[1]),
            "baseline_info": baseline_info,
            "latest_seasonal": latest_seasonal,
            "risk": compute_risk_score(current, latest_seasonal, n_active or 0),
        },
    )


@router.post("/recompute", response_class=HTMLResponse)
async def drought_recompute(request: Request, db: AsyncSession = Depends(get_db)):
    """Admin action: recompute all SPI records from CHIRPS data."""
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    from app.core.database import AsyncSessionLocal
    from app.core.spi import recompute_spi

    async def _do():
        from app.core.spi import recompute_and_evaluate
        async with AsyncSessionLocal() as sdb:
            n_spi, n_act = await recompute_and_evaluate(sdb)
            logger.info("Admin triggered SPI recompute: %d records, %d activation(s)", n_spi, n_act)

    enqueue(_do())
    return RedirectResponse("/drought?recomputed=1", status_code=303)
