import csv
import io
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_action
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord

router = APIRouter(prefix="/impacts")
templates = Jinja2Templates(directory="app/templates")

HAZARD_TYPES = ["flood", "storm", "drought", "landslide", "heatwave", "cyclone", "other"]


PAGE_SIZE = 20


def _build_page_range(current: int, total_pages: int) -> list:
    if total_pages <= 7:
        return list(range(1, total_pages + 1))
    pages: list = []
    shown = sorted({1, total_pages, *range(max(1, current - 2), min(total_pages, current + 2) + 1)})
    prev = 0
    for p in shown:
        if p - prev > 1:
            pages.append(None)
        pages.append(p)
        prev = p
    return pages


@router.get("", response_class=HTMLResponse)
async def impact_list(request: Request, db: AsyncSession = Depends(get_db), page: int = 1):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    from sqlalchemy import func

    page = max(1, page)
    total = await db.scalar(select(func.count()).select_from(ImpactRecord))
    total_pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, total_pages)

    result = await db.execute(
        select(ImpactRecord).order_by(desc(ImpactRecord.event_date))
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    )
    impacts = result.scalars().all()

    return templates.TemplateResponse(
        "impact_list.html",
        {
            "request": request, "user": user, "impacts": impacts,
            "page": page, "total": total, "total_pages": total_pages,
            "page_size": PAGE_SIZE, "page_range": _build_page_range(page, total_pages),
        },
    )


@router.get("/export.csv")
async def impact_export(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(ImpactRecord).order_by(desc(ImpactRecord.event_date)))
    impacts = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "event_name", "event_date", "hazard_type",
        "country", "region", "lat", "lon",
        "affected_population", "casualties", "displaced", "damage_usd",
        "description", "forecast_id", "created_at",
    ])
    for imp in impacts:
        writer.writerow([
            imp.id, imp.event_name, imp.event_date, imp.hazard_type,
            imp.country, imp.region or "", imp.lat or "", imp.lon or "",
            imp.affected_population or "", imp.casualties or "",
            imp.displaced or "", imp.damage_usd or "",
            imp.description or "", imp.forecast_id or "",
            imp.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=impacts.csv"},
    )


@router.get("/new", response_class=HTMLResponse)
async def impact_new_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    forecasts_result = await db.execute(
        select(ForecastUpload).order_by(desc(ForecastUpload.uploaded_at))
    )
    forecasts = forecasts_result.scalars().all()

    return templates.TemplateResponse(
        "impact_form.html",
        {"request": request, "user": user, "forecasts": forecasts, "hazard_types": HAZARD_TYPES},
    )


@router.post("/new")
async def impact_create(
    request: Request,
    event_name: str = Form(...),
    event_date: date = Form(...),
    hazard_type: str = Form(...),
    country: str = Form(...),
    region: str = Form(""),
    lat: str = Form(""),
    lon: str = Form(""),
    affected_population: str = Form(""),
    casualties: str = Form(""),
    displaced: str = Form(""),
    damage_usd: str = Form(""),
    description: str = Form(""),
    forecast_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    def _int(v: str) -> Optional[int]:
        return int(v) if v.strip() else None

    def _float(v: str) -> Optional[float]:
        return float(v) if v.strip() else None

    record = ImpactRecord(
        event_name=event_name,
        event_date=event_date,
        hazard_type=hazard_type,
        country=country,
        region=region or None,
        lat=_float(lat),
        lon=_float(lon),
        affected_population=_int(affected_population),
        casualties=_int(casualties),
        displaced=_int(displaced),
        damage_usd=_float(damage_usd),
        description=description or None,
        forecast_id=_int(forecast_id),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    await log_action(db, user.id, "impact.create", f"Created impact record '{event_name}' ({hazard_type}, {country})")

    return RedirectResponse(f"/impacts/{record.id}", status_code=303)


@router.get("/{impact_id}", response_class=HTMLResponse)
async def impact_detail(impact_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(ImpactRecord).where(ImpactRecord.id == impact_id))
    impact = result.scalar_one_or_none()
    if not impact:
        return RedirectResponse("/impacts")

    return templates.TemplateResponse(
        "impact_detail.html", {"request": request, "user": user, "impact": impact}
    )


@router.get("/{impact_id}/edit", response_class=HTMLResponse)
async def impact_edit_page(impact_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(ImpactRecord).where(ImpactRecord.id == impact_id))
    impact = result.scalar_one_or_none()
    if not impact:
        return RedirectResponse("/impacts")

    forecasts_result = await db.execute(
        select(ForecastUpload).order_by(desc(ForecastUpload.uploaded_at))
    )
    forecasts = forecasts_result.scalars().all()

    return templates.TemplateResponse(
        "impact_form.html",
        {
            "request": request,
            "user": user,
            "impact": impact,
            "forecasts": forecasts,
            "hazard_types": HAZARD_TYPES,
        },
    )


@router.post("/{impact_id}/edit")
async def impact_update(
    impact_id: int,
    request: Request,
    event_name: str = Form(...),
    event_date: date = Form(...),
    hazard_type: str = Form(...),
    country: str = Form(...),
    region: str = Form(""),
    lat: str = Form(""),
    lon: str = Form(""),
    affected_population: str = Form(""),
    casualties: str = Form(""),
    displaced: str = Form(""),
    damage_usd: str = Form(""),
    description: str = Form(""),
    forecast_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(ImpactRecord).where(ImpactRecord.id == impact_id))
    impact = result.scalar_one_or_none()
    if not impact:
        return RedirectResponse("/impacts")

    def _int(v: str) -> Optional[int]:
        return int(v) if v.strip() else None

    def _float(v: str) -> Optional[float]:
        return float(v) if v.strip() else None

    impact.event_name = event_name
    impact.event_date = event_date
    impact.hazard_type = hazard_type
    impact.country = country
    impact.region = region or None
    impact.lat = _float(lat)
    impact.lon = _float(lon)
    impact.affected_population = _int(affected_population)
    impact.casualties = _int(casualties)
    impact.displaced = _int(displaced)
    impact.damage_usd = _float(damage_usd)
    impact.description = description or None
    impact.forecast_id = _int(forecast_id)

    await db.commit()
    await log_action(db, user.id, "impact.edit", f"Edited impact record '{event_name}'")
    return RedirectResponse(f"/impacts/{impact_id}", status_code=303)


@router.post("/{impact_id}/delete")
async def impact_delete(impact_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(ImpactRecord).where(ImpactRecord.id == impact_id))
    impact = result.scalar_one_or_none()
    if impact:
        ename = impact.event_name
        await db.delete(impact)
        await db.commit()
        await log_action(db, user.id, "impact.delete", f"Deleted impact record '{ename}'")

    return RedirectResponse("/impacts", status_code=303)
