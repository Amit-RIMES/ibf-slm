"""Return period / exceedance probability analysis from CHIRPS historical data."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.return_period import (
    STANDARD_RETURN_PERIODS,
    extract_annual_maxima,
    fit_gev,
    return_level,
    return_period_for_value,
    rp_color,
    rp_label,
)
from app.models.observed_rainfall import ObservedRainfall
from app.models.return_level import ReturnLevel

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_FORECAST_PRECIP_VARS = ["precip_mean", "precip_max", "precip_min"]
_VARIABLE_LABELS = {
    "precip_mean": "Mean Precipitation",
    "precip_max": "Max Precipitation",
    "precip_min": "Min Precipitation",
}


async def _compute_and_store(db: AsyncSession) -> dict[str, str]:
    """Fit GEV to annual maxima for each precipitation variable and upsert ReturnLevel rows."""
    obs_r = await db.execute(
        select(ObservedRainfall).order_by(ObservedRainfall.obs_date)
    )
    obs_records = obs_r.scalars().all()

    messages: dict[str, str] = {}
    for var in _FORECAST_PRECIP_VARS:
        by_year = extract_annual_maxima(obs_records, var)
        n_obs = sum(1 for r in obs_records if getattr(r, var, None) is not None)
        annual_maxima = list(by_year.values())
        n_years = len(annual_maxima)

        existing = await db.scalar(
            select(ReturnLevel).where(ReturnLevel.variable == var)
        )

        if not existing:
            existing = ReturnLevel(variable=var)
            db.add(existing)

        existing.computed_at = datetime.now(timezone.utc)
        existing.n_years = n_years
        existing.n_obs = n_obs

        params = fit_gev(annual_maxima)
        if params:
            shape, loc, scale = params
            existing.gev_shape = shape
            existing.gev_loc = loc
            existing.gev_scale = scale
            for rp_yr in STANDARD_RETURN_PERIODS:
                attr = f"rl_{rp_yr}"
                setattr(existing, attr, round(return_level(shape, loc, scale, rp_yr), 2))
            messages[var] = f"OK ({n_years} years, {n_obs} obs)"
        else:
            existing.gev_shape = None
            existing.gev_loc = None
            existing.gev_scale = None
            for rp_yr in STANDARD_RETURN_PERIODS:
                setattr(existing, f"rl_{rp_yr}", None)
            messages[var] = f"Insufficient data ({n_years} years)"

    await db.commit()
    return messages


@router.get("/return-period", response_class=HTMLResponse)
async def return_period_page(
    request: Request,
    computed: str = "",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    rl_r = await db.execute(select(ReturnLevel).order_by(ReturnLevel.variable))
    return_levels = rl_r.scalars().all()

    obs_count_r = await db.execute(select(ObservedRainfall))
    obs_count = len(obs_count_r.scalars().all())

    return templates.TemplateResponse(
        request,
        "return_period.html",
        {
            "user": user,
            "return_levels": return_levels,
            "obs_count": obs_count,
            "var_labels": _VARIABLE_LABELS,
            "return_periods": STANDARD_RETURN_PERIODS,
            "computed": computed,
        },
    )


@router.post("/return-period/compute", response_class=HTMLResponse)
async def compute_return_levels(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return HTMLResponse("Forbidden", status_code=403)

    await _compute_and_store(db)
    return RedirectResponse("/return-period?computed=1", status_code=303)


@router.get("/api/v1/return-period", response_class=JSONResponse)
async def api_return_period(
    value: float,
    variable: str = "precip_mean",
    db: AsyncSession = Depends(get_db),
):
    """Return the return period (years) for a given value and variable."""
    rl = await db.scalar(select(ReturnLevel).where(ReturnLevel.variable == variable))
    if not rl or rl.gev_shape is None:
        return JSONResponse({"error": "No return level data for this variable"}, status_code=404)

    rp = return_period_for_value(rl.gev_shape, rl.gev_loc, rl.gev_scale, value)
    return {
        "variable": variable,
        "value": value,
        "return_period_years": rp,
        "label": rp_label(rp),
        "color": rp_color(rp),
    }


@router.get("/api/v1/return-levels", response_class=JSONResponse)
async def api_return_levels(
    variable: str = "precip_mean",
    db: AsyncSession = Depends(get_db),
):
    """Return the full return level table and GEV parameters for a variable."""
    rl = await db.scalar(select(ReturnLevel).where(ReturnLevel.variable == variable))
    if not rl:
        return JSONResponse({"error": "No data"}, status_code=404)

    return {
        "variable": rl.variable,
        "n_years": rl.n_years,
        "gev_shape": rl.gev_shape,
        "gev_loc": rl.gev_loc,
        "gev_scale": rl.gev_scale,
        "return_levels": {
            str(rp): getattr(rl, f"rl_{rp}")
            for rp in STANDARD_RETURN_PERIODS
        },
        "computed_at": rl.computed_at.isoformat() if rl.computed_at else None,
    }
