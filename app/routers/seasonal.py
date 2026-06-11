from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
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
