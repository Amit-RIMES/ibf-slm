from collections import defaultdict
from datetime import date, datetime, timezone
from itertools import groupby

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.observed_rainfall import ObservedRainfall
from app.models.seasonal import SeasonalForecast

router = APIRouter(prefix="/seasonal")
templates = Jinja2Templates(directory="app/templates")

_FORBIDDEN = HTMLResponse(
    "<h1 style='font-family:system-ui;margin:3rem auto;max-width:400px'>403 — Admin access required</h1>",
    status_code=403,
)

_SOURCES = ["IRI", "ECMWF-SEAS5", "RIMES", "NCEP-CFSv2", "Custom"]


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def seasonal_list(
    request: Request,
    page: int = 1,
    source: str = "",
    variable: str = "",
    year: str = "",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    q = select(SeasonalForecast).order_by(SeasonalForecast.issue_date.desc())
    if source:
        q = q.where(SeasonalForecast.source == source)
    if variable:
        q = q.where(SeasonalForecast.variable == variable)
    if year:
        try:
            yr = int(year)
            q = q.where(
                SeasonalForecast.valid_start >= date(yr, 1, 1),
                SeasonalForecast.valid_start <= date(yr, 12, 31),
            )
        except ValueError:
            pass

    limit = 25
    offset = (page - 1) * limit
    total_r = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_r.scalar_one()
    pages = max(1, (total + limit - 1) // limit)

    rows_r = await db.execute(q.offset(offset).limit(limit))
    rows = rows_r.scalars().all()

    sources_r = await db.execute(select(SeasonalForecast.source).distinct())
    all_sources = sorted({r[0] for r in sources_r.all()} | set(_SOURCES))

    return templates.TemplateResponse(
        request, "seasonal_list.html",
        {
            "user": user,
            "rows": rows,
            "page": page,
            "pages": pages,
            "total": total,
            "source": source,
            "variable": variable,
            "year": year,
            "all_sources": all_sources,
        },
    )


# ── Create form ───────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
async def seasonal_new(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN
    return templates.TemplateResponse(
        request, "seasonal_form.html",
        {"user": user, "sf": None, "sources": _SOURCES, "errors": []},
    )


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", response_class=HTMLResponse)
async def seasonal_create(
    request: Request,
    source: str = Form(...),
    issue_date: str = Form(...),
    valid_start: str = Form(...),
    valid_end: str = Form(...),
    variable: str = Form("precip"),
    below_normal_pct: str = Form(""),
    near_normal_pct: str = Form(""),
    above_normal_pct: str = Form(""),
    precip_anomaly_pct: str = Form(""),
    region_label: str = Form(""),
    lat_min: str = Form(""),
    lat_max: str = Form(""),
    lon_min: str = Form(""),
    lon_max: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    errors = []

    def _date(s: str, field: str):
        try:
            return date.fromisoformat(s.strip())
        except ValueError:
            errors.append(f"Invalid {field} date.")
            return None

    def _float(s: str):
        s = s.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    issue = _date(issue_date, "issue")
    vs = _date(valid_start, "valid start")
    ve = _date(valid_end, "valid end")

    if issue and vs and ve and vs > ve:
        errors.append("Valid end must be on or after valid start.")

    bn = _float(below_normal_pct)
    nn = _float(near_normal_pct)
    an = _float(above_normal_pct)

    if bn is not None and nn is not None and an is not None:
        total = bn + nn + an
        if abs(total - 100) > 1:
            errors.append(f"Tercile percentages must sum to 100 (got {total:.1f}).")

    if not source.strip():
        errors.append("Source is required.")

    if errors:
        return templates.TemplateResponse(
            request, "seasonal_form.html",
            {"user": user, "sf": None, "sources": _SOURCES, "errors": errors},
            status_code=422,
        )

    sf = SeasonalForecast(
        source=source.strip(),
        issue_date=issue,
        valid_start=vs,
        valid_end=ve,
        variable=variable,
        below_normal_pct=bn,
        near_normal_pct=nn,
        above_normal_pct=an,
        precip_anomaly_pct=_float(precip_anomaly_pct),
        region_label=region_label.strip() or None,
        lat_min=_float(lat_min),
        lat_max=_float(lat_max),
        lon_min=_float(lon_min),
        lon_max=_float(lon_max),
        notes=notes.strip() or None,
        uploaded_at=datetime.now(timezone.utc),
        uploaded_by_id=user.id,
    )
    db.add(sf)
    await db.commit()
    await db.refresh(sf)
    return RedirectResponse(f"/seasonal/{sf.id}", status_code=303)


# ── Detail ────────────────────────────────────────────────────────────────────

def _percentile(sorted_list: list, pct: float) -> float | None:
    if not sorted_list:
        return None
    k = (len(sorted_list) - 1) * pct / 100.0
    f = int(k)
    c = min(f + 1, len(sorted_list) - 1)
    return sorted_list[f] + (sorted_list[c] - sorted_list[f]) * (k - f)


@router.get("/skill", response_class=HTMLResponse)
async def seasonal_skill(
    request: Request,
    source: str = "",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    today = date.today()

    # All completed forecasts with tercile probs
    q = (
        select(SeasonalForecast)
        .where(SeasonalForecast.valid_end <= today)
        .where(SeasonalForecast.below_normal_pct.isnot(None))
        .order_by(SeasonalForecast.valid_start.desc())
    )
    if source:
        q = q.where(SeasonalForecast.source == source)
    forecasts = (await db.execute(q)).scalars().all()

    # Batch-fetch all observed rainfall once
    all_obs_r = (await db.execute(
        select(ObservedRainfall.obs_date, ObservedRainfall.precip_mean)
    )).all()

    # Average precip per date (in case multiple sources for same date)
    obs_by_date: dict[date, list] = defaultdict(list)
    for obs in all_obs_r:
        obs_by_date[obs.obs_date].append(obs.precip_mean)
    obs_daily = {d: sum(v) / len(v) for d, v in obs_by_date.items()}

    # Month -> sorted precip list (for reference tercile boundaries)
    month_precips: dict[int, list] = defaultdict(list)
    for d, p in obs_daily.items():
        month_precips[d.month].append(p)
    for m in month_precips:
        month_precips[m].sort()

    RPS_CLIM = 2.0 / 9.0

    rows = []
    for sf in forecasts:
        # Mean observed precip over the valid period
        period_precips = [
            obs_daily[d] for d in obs_daily
            if sf.valid_start <= d <= sf.valid_end
        ]
        if not period_precips:
            continue
        period_obs = sum(period_precips) / len(period_precips)

        # Reference percentile boundaries from all months in the valid period
        months_in_period: set[int] = set()
        cur = sf.valid_start.replace(day=1)
        while cur <= sf.valid_end:
            months_in_period.add(cur.month)
            if cur.month == 12:
                cur = cur.replace(year=cur.year + 1, month=1)
            else:
                cur = cur.replace(month=cur.month + 1)

        ref_precips: list[float] = []
        for m in months_in_period:
            ref_precips.extend(month_precips.get(m, []))
        ref_precips.sort()

        p33 = _percentile(ref_precips, 33.3)
        p67 = _percentile(ref_precips, 66.7)
        if p33 is None or p67 is None or p33 >= p67:
            continue

        # Observed tercile category
        if period_obs <= p33:
            obs_cat = 0  # below normal
        elif period_obs <= p67:
            obs_cat = 1  # near normal
        else:
            obs_cat = 2  # above normal

        # Normalised forecast probabilities
        b = (sf.below_normal_pct or 0) / 100.0
        n = (sf.near_normal_pct or 0) / 100.0
        a = (sf.above_normal_pct or 0) / 100.0
        total = b + n + a
        if total > 0:
            b, n, a = b / total, n / total, a / total
        cum_f = [b, b + n]

        # Cumulative observed outcome
        cum_o = [1.0 if obs_cat == 0 else 0.0, 1.0 if obs_cat <= 1 else 0.0]

        rps = ((cum_f[0] - cum_o[0]) ** 2 + (cum_f[1] - cum_o[1]) ** 2) / 2.0
        rpss = 1.0 - rps / RPS_CLIM

        # Hit: was the highest-probability tercile the correct one?
        predicted_cat = [b, n, a].index(max(b, n, a))
        hit = predicted_cat == obs_cat

        rows.append({
            "id": sf.id,
            "source": sf.source,
            "region": sf.region_label or "—",
            "valid_start": sf.valid_start.strftime("%b %Y"),
            "valid_end": sf.valid_end.strftime("%b %Y"),
            "below_pct": round(sf.below_normal_pct or 0),
            "near_pct": round(sf.near_normal_pct or 0),
            "above_pct": round(sf.above_normal_pct or 0),
            "obs_precip": round(period_obs, 1),
            "obs_cat": ["Below", "Near", "Above"][obs_cat],
            "obs_cat_idx": obs_cat,
            "p33": round(p33, 1),
            "p67": round(p67, 1),
            "rps": round(rps, 4),
            "rpss": round(rpss, 3),
            "hit": hit,
        })

    # Per-source summary
    source_stats = []
    rows_by_src = sorted(rows, key=lambda r: r["source"])
    for src, grp in groupby(rows_by_src, key=lambda r: r["source"]):
        g = list(grp)
        n_src = len(g)
        mean_rpss = sum(r["rpss"] for r in g) / n_src
        hr = sum(1 for r in g if r["hit"]) / n_src
        source_stats.append({
            "source": src,
            "n": n_src,
            "mean_rpss": round(mean_rpss, 3),
            "hit_rate": round(hr * 100),
            "skill_label": "Skillful" if mean_rpss > 0 else "No skill",
            "skill_color": "#16a34a" if mean_rpss > 0 else "#dc2626",
        })

    n_total = len(rows)
    mean_rpss_all = sum(r["rpss"] for r in rows) / n_total if n_total else 0.0
    hit_rate_all = round(sum(1 for r in rows if r["hit"]) / n_total * 100) if n_total else 0

    # Sources available for filter dropdown
    avail_src_r = await db.execute(
        select(SeasonalForecast.source)
        .distinct()
        .where(SeasonalForecast.valid_end <= today)
        .where(SeasonalForecast.below_normal_pct.isnot(None))
    )
    available_sources = sorted(r[0] for r in avail_src_r.all())

    return templates.TemplateResponse(
        request,
        "seasonal_skill.html",
        {
            "user": user,
            "rows": rows,
            "source_stats": source_stats,
            "n_total": n_total,
            "mean_rpss": round(mean_rpss_all, 3),
            "hit_rate": hit_rate_all,
            "source": source,
            "available_sources": available_sources,
        },
    )


@router.get("/{sf_id}", response_class=HTMLResponse)
async def seasonal_detail(sf_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    result = await db.execute(select(SeasonalForecast).where(SeasonalForecast.id == sf_id))
    sf = result.scalar_one_or_none()
    if not sf:
        return RedirectResponse("/seasonal", status_code=303)

    return templates.TemplateResponse(
        request, "seasonal_detail.html",
        {"user": user, "sf": sf},
    )


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/{sf_id}/delete", response_class=HTMLResponse)
async def seasonal_delete(sf_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    result = await db.execute(select(SeasonalForecast).where(SeasonalForecast.id == sf_id))
    sf = result.scalar_one_or_none()
    if sf:
        await db.delete(sf)
        await db.commit()
    return RedirectResponse("/seasonal", status_code=303)
