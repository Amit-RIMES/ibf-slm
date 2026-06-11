import json
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.forecast import ForecastUpload
from app.models.observed_rainfall import ObservedRainfall
from app.models.trigger import Trigger, TriggerActivation

router = APIRouter(prefix="/observed")
templates = Jinja2Templates(directory="app/templates")

_FORBIDDEN = HTMLResponse(
    "<h1 style='font-family:system-ui;margin:3rem auto;max-width:400px'>403 — Admin access required</h1>",
    status_code=403,
)


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def observed_list(
    request: Request,
    page: int = 1,
    source: str = "",
    date_from: str = "",
    date_to: str = "",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    q = select(ObservedRainfall).order_by(ObservedRainfall.obs_date.desc())
    if source:
        q = q.where(ObservedRainfall.source == source)
    if date_from:
        try:
            q = q.where(ObservedRainfall.obs_date >= date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            q = q.where(ObservedRainfall.obs_date <= date.fromisoformat(date_to))
        except ValueError:
            pass

    limit = 30
    offset = (page - 1) * limit
    total_r = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_r.scalar_one()
    pages = max(1, (total + limit - 1) // limit)

    rows_r = await db.execute(q.offset(offset).limit(limit))
    rows = rows_r.scalars().all()

    # Recent 30 records for the mini chart (newest first → reverse for chart)
    chart_r = await db.execute(
        select(ObservedRainfall)
        .order_by(ObservedRainfall.obs_date.desc())
        .limit(30)
    )
    chart_data = list(reversed(chart_r.scalars().all()))

    return templates.TemplateResponse(
        request, "observed_list.html",
        {
            "user": user,
            "rows": rows,
            "page": page,
            "pages": pages,
            "total": total,
            "source": source,
            "date_from": date_from,
            "date_to": date_to,
            "chart_data": chart_data,
        },
    )


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{obs_id}", response_class=HTMLResponse)
async def observed_detail(obs_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    result = await db.execute(select(ObservedRainfall).where(ObservedRainfall.id == obs_id))
    obs = result.scalar_one_or_none()
    if not obs:
        return RedirectResponse("/observed", status_code=303)

    return templates.TemplateResponse(
        request, "observed_detail.html",
        {"user": user, "obs": obs},
    )


# ── Verification dashboard ────────────────────────────────────────────────────

@router.get("/verify/dashboard", response_class=HTMLResponse)
async def observed_verify(
    request: Request,
    days: int = 30,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)

    # All observations in window
    obs_r = await db.execute(
        select(ObservedRainfall)
        .where(ObservedRainfall.obs_date >= cutoff)
        .order_by(ObservedRainfall.obs_date)
    )
    observations = obs_r.scalars().all()

    # Forecasts uploaded in the window (use uploaded_at as proxy for valid date)
    fc_r = await db.execute(
        select(ForecastUpload)
        .where(ForecastUpload.uploaded_at >= datetime.combine(cutoff, datetime.min.time()))
        .order_by(ForecastUpload.uploaded_at)
    )
    forecasts = fc_r.scalars().all()

    # Build a date → observed_mean lookup
    obs_by_date: dict[date, float] = {o.obs_date: o.precip_mean for o in observations}

    # Match each forecast to the observed mean for the day after upload (lead-1 proxy)
    pairs = []
    for fc in forecasts:
        fc_date = fc.uploaded_at.date() + timedelta(days=1)
        obs_mean = obs_by_date.get(fc_date)
        if obs_mean is not None:
            pairs.append({
                "date": fc_date.isoformat(),
                "forecast_mean": round(fc.precip_mean, 2),
                "obs_mean": round(obs_mean, 2),
                "bias": round(fc.precip_mean - obs_mean, 2),
                "abs_error": round(abs(fc.precip_mean - obs_mean), 2),
                "source": fc.source or "",
            })

    # Summary stats
    if pairs:
        biases = [p["bias"] for p in pairs]
        abs_errors = [p["abs_error"] for p in pairs]
        fc_vals = [p["forecast_mean"] for p in pairs]
        ob_vals = [p["obs_mean"] for p in pairs]
        mean_bias = round(sum(biases) / len(biases), 2)
        mae = round(sum(abs_errors) / len(abs_errors), 2)
        rmse = round((sum(b**2 for b in biases) / len(biases)) ** 0.5, 2)
        # Pearson correlation (simple)
        n = len(pairs)
        mx = sum(fc_vals) / n
        my = sum(ob_vals) / n
        num = sum((f - mx) * (o - my) for f, o in zip(fc_vals, ob_vals))
        den = (sum((f - mx)**2 for f in fc_vals) * sum((o - my)**2 for o in ob_vals)) ** 0.5
        corr = round(num / den, 3) if den > 0 else None
    else:
        mean_bias = mae = rmse = corr = None

    # Per-trigger contingency table
    triggers_r = await db.execute(select(Trigger).where(Trigger.is_active == True))
    triggers = triggers_r.scalars().all()

    contingency = []
    for trig in triggers:
        acts_r = await db.execute(
            select(TriggerActivation)
            .where(
                TriggerActivation.trigger_id == trig.id,
                TriggerActivation.triggered_at >= datetime.combine(cutoff, datetime.min.time()),
            )
        )
        activations = acts_r.scalars().all()

        hits = false_alarms = misses = correct_negatives = 0
        for act in activations:
            act_date = act.triggered_at.date()
            obs_mean = obs_by_date.get(act_date)
            if obs_mean is None:
                continue
            forecast_fired = True  # the activation exists
            obs_fired = obs_mean >= trig.threshold
            if forecast_fired and obs_fired:
                hits += 1
            elif forecast_fired and not obs_fired:
                false_alarms += 1
            elif not forecast_fired and obs_fired:
                misses += 1
            else:
                correct_negatives += 1

        total_acts = hits + false_alarms + misses + correct_negatives
        pod = round(hits / (hits + misses), 2) if (hits + misses) > 0 else None
        far = round(false_alarms / (hits + false_alarms), 2) if (hits + false_alarms) > 0 else None
        csi = round(hits / (hits + misses + false_alarms), 2) if (hits + misses + false_alarms) > 0 else None

        contingency.append({
            "trigger": trig,
            "hits": hits,
            "false_alarms": false_alarms,
            "misses": misses,
            "correct_negatives": correct_negatives,
            "total": total_acts,
            "pod": pod,
            "far": far,
            "csi": csi,
        })

    return templates.TemplateResponse(
        request, "observed_verify.html",
        {
            "user": user,
            "days": days,
            "observations": observations,
            "pairs": pairs,
            "mean_bias": mean_bias,
            "mae": mae,
            "rmse": rmse,
            "corr": corr,
            "contingency": contingency,
            "obs_count": len(observations),
        },
    )


# ── Manual sync trigger (admin) ───────────────────────────────────────────────

@router.post("/sync", response_class=HTMLResponse)
async def observed_sync(
    request: Request,
    lookback_days: int = Form(7),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    from app.core.chirps import sync_recent_days
    from app.core.database import AsyncSessionLocal
    from app.core.background import enqueue

    _lbd = lookback_days

    async def _do_sync():
        async with AsyncSessionLocal() as sync_db:
            await sync_recent_days(
                sync_db,
                lookback_days=_lbd,
                lat_min=settings.CHIRPS_LAT_MIN,
                lat_max=settings.CHIRPS_LAT_MAX,
                lon_min=settings.CHIRPS_LON_MIN,
                lon_max=settings.CHIRPS_LON_MAX,
            )

    enqueue(_do_sync())
    return RedirectResponse("/observed?synced=1", status_code=303)
