import calendar
import json
from collections import defaultdict
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


# ── Data completeness calendar ────────────────────────────────────────────────

@router.get("/calendar", response_class=HTMLResponse)
async def observed_calendar(
    request: Request,
    year: int = 0,
    source: str = "CHIRPS",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    today = date.today()
    if not year:
        year = today.year

    # All obs for the selected year and source
    rows_r = await db.execute(
        select(
            ObservedRainfall.obs_date,
            ObservedRainfall.is_preliminary,
            ObservedRainfall.precip_mean,
        )
        .where(
            ObservedRainfall.obs_date >= date(year, 1, 1),
            ObservedRainfall.obs_date <= date(year, 12, 31),
            ObservedRainfall.source == source,
        )
    )
    obs_map: dict[date, dict] = {
        r.obs_date: {"preliminary": r.is_preliminary, "precip": r.precip_mean}
        for r in rows_r.all()
    }

    # Build calendar grid: list of months, each with a grid of week-rows
    months = []
    total_days = 0
    present_final = 0
    present_prelim = 0
    gaps = 0

    for month_num in range(1, 13):
        _, days_in_month = calendar.monthrange(year, month_num)
        first_weekday = date(year, month_num, 1).weekday()  # 0=Mon

        cells = []
        # leading empty cells
        cells.extend([None] * first_weekday)
        for day in range(1, days_in_month + 1):
            d = date(year, month_num, day)
            if d > today:
                cells.append({"date": d, "status": "future", "precip": None})
            elif d in obs_map:
                rec = obs_map[d]
                status = "prelim" if rec["preliminary"] else "ok"
                cells.append({"date": d, "status": status, "precip": round(rec["precip"], 1)})
                if rec["preliminary"]:
                    present_prelim += 1
                else:
                    present_final += 1
                total_days += 1
            else:
                cells.append({"date": d, "status": "gap", "precip": None})
                gaps += 1
                total_days += 1

        # Chunk into weeks
        weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
        months.append({
            "name": calendar.month_name[month_num],
            "num": month_num,
            "weeks": weeks,
        })

    # Find longest gap
    all_past = sorted(
        d for d in (date(year, m, day) for m in range(1, 13)
                    for day in range(1, calendar.monthrange(year, m)[1] + 1)
                    if date(year, m, day) <= today)
    )
    max_gap = 0
    cur_gap = 0
    last_gap_end = None
    for d in all_past:
        if d in obs_map:
            if cur_gap > max_gap:
                max_gap = cur_gap
            cur_gap = 0
        else:
            cur_gap += 1
            last_gap_end = d
    if cur_gap > max_gap:
        max_gap = cur_gap

    # Recent gap dates (last 14 missing days) for the gap list
    recent_gaps = [d for d in reversed(all_past) if d not in obs_map][:14]

    # Available sources for the filter
    src_r = await db.execute(select(ObservedRainfall.source).distinct())
    available_sources = sorted(r[0] for r in src_r.all()) or ["CHIRPS"]

    # Year range: first year with data → current year
    yr_range_r = await db.execute(
        select(
            func.min(ObservedRainfall.obs_date),
            func.max(ObservedRainfall.obs_date),
        ).where(ObservedRainfall.source == source)
    )
    yr_row = yr_range_r.one()
    first_yr = yr_row[0].year if yr_row[0] else today.year
    last_yr = today.year
    year_range = list(range(first_yr, last_yr + 1))

    coverage_pct = round(present_final / total_days * 100) if total_days else 0

    return templates.TemplateResponse(
        request, "observed_calendar.html",
        {
            "user": user,
            "year": year,
            "source": source,
            "months": months,
            "total_days": total_days,
            "present_final": present_final,
            "present_prelim": present_prelim,
            "gaps": gaps,
            "coverage_pct": coverage_pct,
            "max_gap": max_gap,
            "recent_gaps": recent_gaps,
            "available_sources": available_sources,
            "year_range": year_range,
            "today": today,
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

    # ── Monthly skill trend (full history, independent of window filter) ──────
    all_obs_r = await db.execute(
        select(ObservedRainfall).order_by(ObservedRainfall.obs_date)
    )
    all_obs_by_date: dict[date, float] = {
        o.obs_date: o.precip_mean for o in all_obs_r.scalars().all()
    }
    all_fc_r = await db.execute(
        select(ForecastUpload).order_by(ForecastUpload.uploaded_at)
    )
    monthly_buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for fc in all_fc_r.scalars().all():
        fc_date = fc.uploaded_at.date() + timedelta(days=1)
        obs_val = all_obs_by_date.get(fc_date)
        if obs_val is not None:
            ym = f"{fc_date.year}-{fc_date.month:02d}"
            monthly_buckets[ym].append((fc.precip_mean, obs_val))

    skill_trend = []
    for ym in sorted(monthly_buckets):
        pts = monthly_buckets[ym]
        n = len(pts)
        biases_m = [f - o for f, o in pts]
        mae_m = round(sum(abs(b) for b in biases_m) / n, 3)
        rmse_m = round((sum(b**2 for b in biases_m) / n) ** 0.5, 3)
        bias_m = round(sum(biases_m) / n, 3)
        yr, mo = int(ym[:4]), int(ym[5:])
        skill_trend.append({
            "label": f"{calendar.month_abbr[mo]} {yr}",
            "mae": mae_m,
            "rmse": rmse_m,
            "bias": bias_m,
            "n": n,
        })

    # ── Performance diagram data (SR vs POD scatter) ─────────────────────────
    perf_points = []
    for c in contingency:
        if c["pod"] is not None and c["far"] is not None:
            sr = round(1 - c["far"], 3)
            perf_points.append({
                "name": c["trigger"].name,
                "hazard": c["trigger"].hazard_type or "other",
                "sr": sr,
                "pod": c["pod"],
                "csi": c["csi"],
                "hits": c["hits"],
                "far": c["far"],
                "misses": c["misses"],
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
            "skill_trend": json.dumps(skill_trend),
            "skill_trend_len": len(skill_trend),
            "perf_points": json.dumps(perf_points),
            "perf_points_len": len(perf_points),
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

    from app.core.background import enqueue
    from app.core.chirps import sync_recent_days
    from app.core.database import AsyncSessionLocal

    _lbd = lookback_days

    async def _do_sync():
        async with AsyncSessionLocal() as sync_db:
            ingested = await sync_recent_days(
                sync_db,
                lookback_days=_lbd,
                lat_min=settings.CHIRPS_LAT_MIN,
                lat_max=settings.CHIRPS_LAT_MAX,
                lon_min=settings.CHIRPS_LON_MIN,
                lon_max=settings.CHIRPS_LON_MAX,
            )
        if ingested:
            from app.core.spi import recompute_and_evaluate
            async with AsyncSessionLocal() as spi_db:
                await recompute_and_evaluate(spi_db)

    enqueue(_do_sync())
    return RedirectResponse("/observed?synced=1", status_code=303)


# ── Historical backfill (admin) ───────────────────────────────────────────────

@router.post("/backfill", response_class=HTMLResponse)
async def observed_backfill(
    request: Request,
    start_date: str = Form(...),
    end_date: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    try:
        from datetime import date as _date
        sd = _date.fromisoformat(start_date)
        ed = _date.fromisoformat(end_date)
    except ValueError:
        return RedirectResponse("/observed?backfill_error=invalid_dates", status_code=303)

    if sd > ed:
        return RedirectResponse("/observed?backfill_error=date_order", status_code=303)

    from app.core.background import enqueue
    from app.core.chirps import backfill_range
    from app.core.database import AsyncSessionLocal
    from app.core.spi import recompute_and_evaluate

    _sd, _ed = sd, ed

    async def _do_backfill():
        async with AsyncSessionLocal() as bf_db:
            ingested, skipped, errors = await backfill_range(
                bf_db, _sd, _ed,
                lat_min=settings.CHIRPS_LAT_MIN,
                lat_max=settings.CHIRPS_LAT_MAX,
                lon_min=settings.CHIRPS_LON_MIN,
                lon_max=settings.CHIRPS_LON_MAX,
            )
        if ingested:
            async with AsyncSessionLocal() as spi_db:
                await recompute_and_evaluate(spi_db)

    enqueue(_do_backfill())
    total_days = (ed - sd).days + 1
    return RedirectResponse(f"/observed?backfill_started={total_days}", status_code=303)
