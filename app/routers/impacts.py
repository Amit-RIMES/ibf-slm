import csv
import io
import json
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_action
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.trigger import TriggerActivation

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
async def impact_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    q: str = "",
    hazard: str = "",
    country: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    from sqlalchemy import and_, func
    from datetime import date as date_type, timedelta

    page = max(1, page)
    filters = []
    if q:
        filters.append(ImpactRecord.event_name.ilike(f"%{q}%"))
    if hazard and hazard in HAZARD_TYPES:
        filters.append(ImpactRecord.hazard_type == hazard)
    if country:
        filters.append(ImpactRecord.country.ilike(f"%{country}%"))
    if date_from:
        try:
            filters.append(ImpactRecord.event_date >= date_type.fromisoformat(date_from))
        except ValueError:
            date_from = ""
    if date_to:
        try:
            filters.append(ImpactRecord.event_date <= date_type.fromisoformat(date_to))
        except ValueError:
            date_to = ""

    base = select(ImpactRecord)
    if filters:
        base = base.where(and_(*filters))

    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    total_pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, total_pages)

    result = await db.execute(
        base.order_by(desc(ImpactRecord.event_date))
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    )
    impacts = result.scalars().all()

    # Map data — all filtered impacts with coordinates (up to 500)
    map_stmt = (
        base.where(ImpactRecord.lat.isnot(None), ImpactRecord.lon.isnot(None))
        .order_by(desc(ImpactRecord.event_date))
        .limit(500)
    )
    map_result = await db.execute(map_stmt)
    map_impacts = map_result.scalars().all()
    map_points = json.dumps([
        {
            "id": imp.id,
            "lat": imp.lat,
            "lon": imp.lon,
            "event_name": imp.event_name,
            "event_date": str(imp.event_date),
            "hazard_type": imp.hazard_type,
            "country": imp.country,
            "affected": imp.affected_population,
            "casualties": imp.casualties,
        }
        for imp in map_impacts
    ])

    return templates.TemplateResponse(
        "impact_list.html",
        {
            "request": request, "user": user, "impacts": impacts,
            "q": q, "hazard": hazard, "country": country,
            "date_from": date_from, "date_to": date_to,
            "hazard_types": HAZARD_TYPES,
            "page": page, "total": total, "total_pages": total_pages,
            "page_size": PAGE_SIZE, "page_range": _build_page_range(page, total_pages),
            "map_points": map_points, "map_count": len(map_impacts),
        },
    )


@router.get("/export.csv")
async def impact_export(
    request: Request,
    db: AsyncSession = Depends(get_db),
    q: str = "",
    hazard: str = "",
    country: str = "",
    date_from: str = "",
    date_to: str = "",
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    from sqlalchemy import and_
    from datetime import date as date_type

    filters = []
    if q:
        filters.append(ImpactRecord.event_name.ilike(f"%{q}%"))
    if hazard and hazard in HAZARD_TYPES:
        filters.append(ImpactRecord.hazard_type == hazard)
    if country:
        filters.append(ImpactRecord.country.ilike(f"%{country}%"))
    if date_from:
        try:
            filters.append(ImpactRecord.event_date >= date_type.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            filters.append(ImpactRecord.event_date <= date_type.fromisoformat(date_to))
        except ValueError:
            pass

    stmt = select(ImpactRecord)
    if filters:
        stmt = stmt.where(and_(*filters))
    stmt = stmt.order_by(desc(ImpactRecord.event_date))

    result = await db.execute(stmt)
    impacts = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "event_name", "event_date", "hazard_type",
        "country", "region", "lat", "lon",
        "affected_population", "casualties", "displaced", "damage_usd",
        "description", "forecast_id", "trigger_activation_id", "created_at",
    ])
    for imp in impacts:
        writer.writerow([
            imp.id, imp.event_name, imp.event_date, imp.hazard_type,
            imp.country, imp.region or "", imp.lat or "", imp.lon or "",
            imp.affected_population or "", imp.casualties or "",
            imp.displaced or "", imp.damage_usd or "",
            imp.description or "", imp.forecast_id or "",
            imp.trigger_activation_id or "",
            imp.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=impacts.csv"},
    )


async def _load_form_data(db: AsyncSession):
    forecasts = (await db.execute(
        select(ForecastUpload).order_by(desc(ForecastUpload.uploaded_at))
    )).scalars().all()
    activations = (await db.execute(
        select(TriggerActivation).order_by(desc(TriggerActivation.triggered_at))
    )).scalars().all()
    return forecasts, activations


@router.get("/new", response_class=HTMLResponse)
async def impact_new_page(request: Request, db: AsyncSession = Depends(get_db), activation: int = 0):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    forecasts, activations = await _load_form_data(db)

    # Pre-fill forecast_id from the activation if navigating from trigger detail
    prefill_forecast_id = None
    if activation:
        act = next((a for a in activations if a.id == activation), None)
        if act:
            prefill_forecast_id = act.forecast_id

    return templates.TemplateResponse(
        "impact_form.html",
        {
            "request": request, "user": user, "forecasts": forecasts,
            "activations": activations, "hazard_types": HAZARD_TYPES,
            "prefill_activation_id": activation or None,
            "prefill_forecast_id": prefill_forecast_id,
        },
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
    trigger_activation_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    def _int(v: str) -> Optional[int]:
        try:
            return int(v) if v.strip() else None
        except ValueError:
            return None

    def _float(v: str) -> Optional[float]:
        try:
            return float(v) if v.strip() else None
        except ValueError:
            return None

    def _err(msg):
        return templates.TemplateResponse(
            "impact_form.html",
            {"request": request, "user": user, "impact": None, "error": msg,
             "forecasts": [], "activations": [], "hazard_types": HAZARD_TYPES},
        )

    lat_val = _float(lat)
    lon_val = _float(lon)
    if lat.strip() and lat_val is None:
        return _err("Latitude must be a number between -90 and 90.")
    if lon.strip() and lon_val is None:
        return _err("Longitude must be a number between -180 and 180.")
    if lat_val is not None and not (-90 <= lat_val <= 90):
        return _err("Latitude must be between -90 and 90.")
    if lon_val is not None and not (-180 <= lon_val <= 180):
        return _err("Longitude must be between -180 and 180.")

    record = ImpactRecord(
        event_name=event_name,
        event_date=event_date,
        hazard_type=hazard_type,
        country=country,
        region=region or None,
        lat=lat_val,
        lon=lon_val,
        affected_population=_int(affected_population),
        casualties=_int(casualties),
        displaced=_int(displaced),
        damage_usd=_float(damage_usd),
        description=description or None,
        forecast_id=_int(forecast_id),
        trigger_activation_id=_int(trigger_activation_id),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    await log_action(db, user.id, "impact.create", f"Created impact record '{event_name}' ({hazard_type}, {country})")

    return RedirectResponse(f"/impacts/{record.id}", status_code=303)


@router.get("/import/template.csv")
async def impact_import_template(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    rows = [
        "event_name,event_date,hazard_type,country,region,lat,lon,"
        "affected_population,casualties,displaced,damage_usd,description,"
        "forecast_id,trigger_activation_id\n",
        "Example flood event,2026-06-01,flood,Philippines,Luzon,14.5,121.0,"
        "5000,2,800,125000.00,Heavy rainfall caused flooding,,\n",
    ]
    return StreamingResponse(
        iter(rows),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=impacts_template.csv"},
    )


@router.get("/import", response_class=HTMLResponse)
async def impact_import_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        "impact_import.html",
        {"request": request, "user": user, "results": None, "error": None,
         "hazard_types": HAZARD_TYPES},
    )


@router.post("/import", response_class=HTMLResponse)
async def impact_import_upload(
    request: Request,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    def _render(results=None, error=None):
        return templates.TemplateResponse(
            "impact_import.html",
            {"request": request, "user": user, "results": results,
             "error": error, "hazard_types": HAZARD_TYPES},
        )

    # Decode file
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except Exception:
            return _render(error="Cannot decode file. Please save as UTF-8 CSV.")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return _render(error="File appears empty or has no header row.")

    # Normalise header names
    fieldnames = [f.strip().lower() for f in reader.fieldnames]
    required = {"event_name", "event_date", "hazard_type", "country"}
    missing = required - set(fieldnames)
    if missing:
        return _render(error=f"Missing required columns: {', '.join(sorted(missing))}. "
                             "Download the template for the correct format.")

    # Pre-load valid FK sets for fast lookup
    from sqlalchemy import func
    fc_ids  = {r[0] for r in (await db.execute(select(ForecastUpload.id))).all()}
    act_ids = {r[0] for r in (await db.execute(select(TriggerActivation.id))).all()}

    def _int(v, key):
        if not v:
            return None, None
        try:
            n = int(float(v))
            return (n, None) if n >= 0 else (None, f"{key} must be non-negative")
        except ValueError:
            return None, f"{key} '{v}' is not a valid integer"

    def _float(v, key):
        if not v:
            return None, None
        try:
            return float(v), None
        except ValueError:
            return None, f"{key} '{v}' is not a valid number"

    imported, skipped = [], []

    for row_num, raw_row in enumerate(reader, start=2):
        row = {k.strip().lower(): (v.strip() if v else "") for k, v in raw_row.items()}
        errors = []

        event_name = row.get("event_name", "")
        if not event_name:
            errors.append("event_name is required")

        event_date_val = None
        eds = row.get("event_date", "")
        if not eds:
            errors.append("event_date is required")
        else:
            try:
                from datetime import date as _date
                event_date_val = _date.fromisoformat(eds)
            except ValueError:
                errors.append(f"event_date '{eds}' must be YYYY-MM-DD")

        hazard = row.get("hazard_type", "").lower()
        if not hazard:
            errors.append("hazard_type is required")
        elif hazard not in HAZARD_TYPES:
            errors.append(f"hazard_type '{hazard}' must be one of: {', '.join(HAZARD_TYPES)}")

        country = row.get("country", "")
        if not country:
            errors.append("country is required")

        affected, err = _int(row.get("affected_population"), "affected_population"); err and errors.append(err)
        casualties, err = _int(row.get("casualties"), "casualties"); err and errors.append(err)
        displaced, err = _int(row.get("displaced"), "displaced"); err and errors.append(err)
        damage, err = _float(row.get("damage_usd"), "damage_usd"); err and errors.append(err)

        lat, err = _float(row.get("lat"), "lat"); err and errors.append(err)
        lon, err = _float(row.get("lon"), "lon"); err and errors.append(err)
        if lat is not None and not (-90 <= lat <= 90):
            errors.append("lat must be between -90 and 90"); lat = None
        if lon is not None and not (-180 <= lon <= 180):
            errors.append("lon must be between -180 and 180"); lon = None
        if (lat is None) != (lon is None) and not any("lat" in e or "lon" in e for e in errors):
            errors.append("lat and lon must both be provided or both omitted")

        forecast_id_val = None
        fid_s = row.get("forecast_id", "")
        if fid_s:
            try:
                fid = int(fid_s)
                if fid not in fc_ids:
                    errors.append(f"forecast_id {fid} does not exist")
                else:
                    forecast_id_val = fid
            except ValueError:
                errors.append(f"forecast_id '{fid_s}' must be an integer")

        act_id_val = None
        aid_s = row.get("trigger_activation_id", "")
        if aid_s:
            try:
                aid = int(aid_s)
                if aid not in act_ids:
                    errors.append(f"trigger_activation_id {aid} does not exist")
                else:
                    act_id_val = aid
            except ValueError:
                errors.append(f"trigger_activation_id '{aid_s}' must be an integer")

        if errors:
            skipped.append({"row": row_num, "event_name": event_name or "—", "errors": errors})
            continue

        db.add(ImpactRecord(
            event_name=event_name,
            event_date=event_date_val,
            hazard_type=hazard,
            country=country,
            region=row.get("region") or None,
            lat=lat, lon=lon,
            affected_population=affected,
            casualties=casualties,
            displaced=displaced,
            damage_usd=damage,
            description=row.get("description") or None,
            forecast_id=forecast_id_val,
            trigger_activation_id=act_id_val,
        ))
        imported.append({"row": row_num, "event_name": event_name})

    if imported:
        await db.commit()
        await log_action(
            db, user.id, "impact.bulk_import",
            f"Bulk imported {len(imported)} impact record{'s' if len(imported) != 1 else ''} "
            f"from {file.filename}",
        )

    return _render(results={
        "filename": file.filename,
        "total": len(imported) + len(skipped),
        "imported": imported,
        "skipped": skipped,
    })


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

    forecasts, activations = await _load_form_data(db)

    return templates.TemplateResponse(
        "impact_form.html",
        {
            "request": request, "user": user, "impact": impact,
            "forecasts": forecasts, "activations": activations,
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
    trigger_activation_id: str = Form(""),
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
        try:
            return int(v) if v.strip() else None
        except ValueError:
            return None

    def _float(v: str) -> Optional[float]:
        try:
            return float(v) if v.strip() else None
        except ValueError:
            return None

    lat_val = _float(lat)
    lon_val = _float(lon)
    if lat.strip() and lat_val is None:
        return templates.TemplateResponse(
            "impact_form.html",
            {"request": request, "user": user, "impact": impact, "error": "Latitude must be a number.",
             "forecasts": [], "activations": [], "hazard_types": HAZARD_TYPES},
        )
    if lon.strip() and lon_val is None:
        return templates.TemplateResponse(
            "impact_form.html",
            {"request": request, "user": user, "impact": impact, "error": "Longitude must be a number.",
             "forecasts": [], "activations": [], "hazard_types": HAZARD_TYPES},
        )
    if lat_val is not None and not (-90 <= lat_val <= 90):
        return templates.TemplateResponse(
            "impact_form.html",
            {"request": request, "user": user, "impact": impact, "error": "Latitude must be between -90 and 90.",
             "forecasts": [], "activations": [], "hazard_types": HAZARD_TYPES},
        )
    if lon_val is not None and not (-180 <= lon_val <= 180):
        return templates.TemplateResponse(
            "impact_form.html",
            {"request": request, "user": user, "impact": impact, "error": "Longitude must be between -180 and 180.",
             "forecasts": [], "activations": [], "hazard_types": HAZARD_TYPES},
        )

    impact.event_name = event_name
    impact.event_date = event_date
    impact.hazard_type = hazard_type
    impact.country = country
    impact.region = region or None
    impact.lat = lat_val
    impact.lon = lon_val
    impact.affected_population = _int(affected_population)
    impact.casualties = _int(casualties)
    impact.displaced = _int(displaced)
    impact.damage_usd = _float(damage_usd)
    impact.description = description or None
    impact.forecast_id = _int(forecast_id)
    impact.trigger_activation_id = _int(trigger_activation_id)

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
