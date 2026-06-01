from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.user import User

router = APIRouter(prefix="/impacts")
templates = Jinja2Templates(directory="app/templates")

HAZARD_TYPES = ["flood", "storm", "drought", "landslide", "heatwave", "cyclone", "other"]


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    result = await db.execute(select(User).where(User.id == int(payload["sub"])))
    return result.scalar_one_or_none()


@router.get("", response_class=HTMLResponse)
async def impact_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(
        select(ImpactRecord).order_by(desc(ImpactRecord.event_date))
    )
    impacts = result.scalars().all()

    return templates.TemplateResponse(
        "impact_list.html", {"request": request, "user": user, "impacts": impacts}
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
