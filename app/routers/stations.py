"""Station/AWS data management and ingestion."""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.station_ingest import parse_csv
from app.core.station_triggers import evaluate_station_triggers
from app.models.station import Station, StationObservation

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_FORBIDDEN = HTMLResponse("Forbidden", status_code=403)


def _float_or_none(s: str) -> float | None:
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


@router.get("/stations", response_class=HTMLResponse)
async def stations_list(
    request: Request,
    country: str = "",
    active_only: str = "",
    flash: str = "",
    error: str = "",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    q = select(Station).order_by(Station.country, Station.name)
    if country:
        q = q.where(Station.country == country)
    if active_only:
        q = q.where(Station.is_active == True)  # noqa: E712

    stations_r = await db.execute(q)
    stations = stations_r.scalars().all()

    # Latest observation per station
    latest_obs: dict[str, StationObservation] = {}
    if stations:
        sids = [s.station_id for s in stations]
        latest_r = await db.execute(
            select(StationObservation)
            .where(StationObservation.station_id.in_(sids))
            .order_by(StationObservation.station_id, desc(StationObservation.obs_date))
        )
        seen: set[str] = set()
        for obs in latest_r.scalars().all():
            if obs.station_id not in seen:
                latest_obs[obs.station_id] = obs
                seen.add(obs.station_id)

    # Observation count per station
    count_r = await db.execute(
        select(StationObservation.station_id, func.count().label("cnt"))
        .group_by(StationObservation.station_id)
    )
    obs_counts: dict[str, int] = {row[0]: row[1] for row in count_r.all()}

    # Distinct countries for filter
    countries_r = await db.execute(
        select(Station.country).distinct().where(Station.country != None)  # noqa: E711
    )
    countries = sorted(r[0] for r in countries_r.all() if r[0])

    return templates.TemplateResponse(
        request,
        "stations.html",
        {
            "user": user,
            "stations": stations,
            "latest_obs": latest_obs,
            "obs_counts": obs_counts,
            "countries": countries,
            "country": country,
            "active_only": active_only,
            "flash": flash,
            "error": error,
        },
    )


@router.post("/stations/add", response_class=HTMLResponse)
async def station_add(
    request: Request,
    station_id: str = Form(...),
    name: str = Form(""),
    country: str = Form(""),
    lat: str = Form(...),
    lon: str = Form(...),
    elevation_m: str = Form(""),
    source: str = Form("manual"),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    sid = station_id.strip().upper()
    if not sid:
        return RedirectResponse("/stations?error=Station+ID+required", status_code=303)

    existing = await db.scalar(select(Station).where(Station.station_id == sid))
    if existing:
        return RedirectResponse(f"/stations?error=Station+{sid}+already+exists", status_code=303)

    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except ValueError:
        return RedirectResponse("/stations?error=Invalid+coordinates", status_code=303)

    db.add(Station(
        station_id=sid,
        name=name.strip(),
        country=country.strip() or None,
        lat=lat_f,
        lon=lon_f,
        elevation_m=_float_or_none(elevation_m),
        source=source.strip() or "manual",
    ))
    await db.commit()
    return RedirectResponse(f"/stations?flash=Station+{sid}+added", status_code=303)


@router.post("/stations/{sid}/toggle", response_class=HTMLResponse)
async def station_toggle(
    sid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    st = await db.scalar(select(Station).where(Station.station_id == sid))
    if st:
        st.is_active = not st.is_active
        await db.commit()
    return RedirectResponse("/stations", status_code=303)


@router.post("/stations/upload-csv", response_class=HTMLResponse)
async def station_upload_csv(
    request: Request,
    csv_file: UploadFile = File(...),
    default_station_id: str = Form(""),
    is_provisional: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    content = await csv_file.read()
    sid_default = default_station_id.strip().upper() or None
    rows, errors = parse_csv(content, default_station_id=sid_default)

    if not rows:
        msg = "; ".join(errors[:5]) if errors else "Empty file"
        return RedirectResponse(f"/stations?error={msg.replace(' ', '+')}", status_code=303)

    provisional = bool(is_provisional)
    inserted = 0
    skipped = 0

    for row in rows:
        sid = row["station_id"].upper()
        obs_date = row["obs_date"]

        # Auto-create station if it doesn't exist (minimal metadata)
        st = await db.scalar(select(Station).where(Station.station_id == sid))
        if not st:
            db.add(Station(station_id=sid, name=sid, lat=0.0, lon=0.0, source="csv_import"))

        existing_obs = await db.scalar(
            select(StationObservation).where(
                StationObservation.station_id == sid,
                StationObservation.obs_date == obs_date,
            )
        )
        if existing_obs:
            skipped += 1
            continue

        db.add(StationObservation(
            station_id=sid,
            obs_date=obs_date,
            precip_mm=row.get("precip_mm"),
            temp_max_c=row.get("temp_max_c"),
            temp_min_c=row.get("temp_min_c"),
            temp_mean_c=row.get("temp_mean_c"),
            humidity_pct=row.get("humidity_pct"),
            wind_speed_ms=row.get("wind_speed_ms"),
            pressure_hpa=row.get("pressure_hpa"),
            source="csv_import",
            is_provisional=provisional,
        ))
        inserted += 1

    await db.commit()
    if inserted and rows:
        latest_date = max(r["obs_date"] for r in rows)
        await evaluate_station_triggers(db, latest_date)
    msg = f"Imported+{inserted}+observations"
    if skipped:
        msg += f",+{skipped}+skipped+(duplicates)"
    return RedirectResponse(f"/stations?flash={msg}", status_code=303)


@router.get("/stations/compare", response_class=HTMLResponse)
async def station_compare(
    request: Request,
    ids: str = "",
    variable: str = "precip_mm",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    VARIABLES = ["precip_mm", "temp_max_c", "temp_min_c", "temp_mean_c",
                 "humidity_pct", "wind_speed_ms", "pressure_hpa"]
    VARIABLE_LABELS = {
        "precip_mm": "Precipitation (mm)",
        "temp_max_c": "Max Temperature (°C)",
        "temp_min_c": "Min Temperature (°C)",
        "temp_mean_c": "Mean Temperature (°C)",
        "humidity_pct": "Humidity (%)",
        "wind_speed_ms": "Wind Speed (m/s)",
        "pressure_hpa": "Pressure (hPa)",
    }
    if variable not in VARIABLES:
        variable = "precip_mm"

    # Load all stations for the selector
    all_stations_r = await db.execute(select(Station).order_by(Station.name))
    all_stations = all_stations_r.scalars().all()

    selected_ids = [s.strip() for s in ids.split(",") if s.strip()][:5]  # max 5 stations

    import json as _json
    series = []
    if selected_ids:
        obs_r = await db.execute(
            select(StationObservation)
            .where(StationObservation.station_id.in_(selected_ids))
            .order_by(StationObservation.obs_date)
        )
        all_obs = obs_r.scalars().all()

        # Build per-station date→value map
        from collections import defaultdict
        by_station: dict[str, dict] = defaultdict(dict)
        all_dates: set = set()
        for obs in all_obs:
            val = getattr(obs, variable)
            if val is not None:
                by_station[obs.station_id][obs.obs_date.isoformat()] = val
                all_dates.add(obs.obs_date.isoformat())

        sorted_dates = sorted(all_dates)
        station_map = {s.station_id: s for s in all_stations}

        for sid in selected_ids:
            st = station_map.get(sid)
            if not st:
                continue
            values = [by_station[sid].get(d) for d in sorted_dates]
            series.append({"id": sid, "name": st.name, "values": values})

    return templates.TemplateResponse(
        request, "station_compare.html",
        {
            "user": user,
            "all_stations": all_stations,
            "selected_ids": selected_ids,
            "variable": variable,
            "variables": VARIABLES,
            "variable_labels": VARIABLE_LABELS,
            "dates_json": _json.dumps(sorted(all_dates) if selected_ids else []),
            "series_json": _json.dumps(series),
        },
    )


@router.get("/stations/{sid}", response_class=HTMLResponse)
async def station_detail(
    sid: str,
    request: Request,
    date_from: str = "",
    date_to: str = "",
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    station = await db.scalar(select(Station).where(Station.station_id == sid))
    if not station:
        return RedirectResponse("/stations", status_code=303)

    q = (
        select(StationObservation)
        .where(StationObservation.station_id == sid)
        .order_by(desc(StationObservation.obs_date))
    )
    if date_from:
        try:
            q = q.where(StationObservation.obs_date >= date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            q = q.where(StationObservation.obs_date <= date.fromisoformat(date_to))
        except ValueError:
            pass

    obs_r = await db.execute(q)
    observations = obs_r.scalars().all()

    # Recent 90 for chart (most recent first → reverse for chart)
    chart_obs = list(reversed(observations[:90]))
    chart_data = [
        {
            "date": str(o.obs_date),
            "precip": o.precip_mm,
            "tmax": o.temp_max_c,
            "tmin": o.temp_min_c,
        }
        for o in chart_obs
    ]

    import json
    chart_json = json.dumps(chart_data)

    return templates.TemplateResponse(
        request,
        "station_detail.html",
        {
            "user": user,
            "station": station,
            "observations": observations,
            "chart_json": chart_json,
            "date_from": date_from,
            "date_to": date_to,
        },
    )


@router.post("/stations/{sid}/obs", response_class=HTMLResponse)
async def station_add_obs(
    sid: str,
    request: Request,
    obs_date: str = Form(...),
    precip_mm: str = Form(""),
    temp_max_c: str = Form(""),
    temp_min_c: str = Form(""),
    temp_mean_c: str = Form(""),
    humidity_pct: str = Form(""),
    wind_speed_ms: str = Form(""),
    pressure_hpa: str = Form(""),
    is_provisional: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "admin":
        return _FORBIDDEN

    station = await db.scalar(select(Station).where(Station.station_id == sid))
    if not station:
        return RedirectResponse("/stations", status_code=303)

    try:
        obs_d = date.fromisoformat(obs_date.strip())
    except ValueError:
        return RedirectResponse(f"/stations/{sid}?error=Invalid+date", status_code=303)

    existing = await db.scalar(
        select(StationObservation).where(
            StationObservation.station_id == sid,
            StationObservation.obs_date == obs_d,
        )
    )
    if existing:
        # Update existing
        for field, val in [
            ("precip_mm", precip_mm), ("temp_max_c", temp_max_c),
            ("temp_min_c", temp_min_c), ("temp_mean_c", temp_mean_c),
            ("humidity_pct", humidity_pct), ("wind_speed_ms", wind_speed_ms),
            ("pressure_hpa", pressure_hpa),
        ]:
            v = _float_or_none(val)
            if v is not None:
                setattr(existing, field, v)
    else:
        db.add(StationObservation(
            station_id=sid,
            obs_date=obs_d,
            precip_mm=_float_or_none(precip_mm),
            temp_max_c=_float_or_none(temp_max_c),
            temp_min_c=_float_or_none(temp_min_c),
            temp_mean_c=_float_or_none(temp_mean_c),
            humidity_pct=_float_or_none(humidity_pct),
            wind_speed_ms=_float_or_none(wind_speed_ms),
            pressure_hpa=_float_or_none(pressure_hpa),
            source="manual",
            is_provisional=bool(is_provisional),
        ))
    await db.commit()
    await evaluate_station_triggers(db, obs_d)
    return RedirectResponse(f"/stations/{sid}", status_code=303)


@router.get("/stations/{sid}/export.csv")
async def station_export_csv(
    sid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    obs_r = await db.execute(
        select(StationObservation)
        .where(StationObservation.station_id == sid)
        .order_by(StationObservation.obs_date)
    )
    observations = obs_r.scalars().all()

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[
        "station_id", "date", "precip_mm", "temp_max_c", "temp_min_c",
        "temp_mean_c", "humidity_pct", "wind_speed_ms", "pressure_hpa",
        "source", "is_provisional",
    ])
    writer.writeheader()
    for o in observations:
        writer.writerow({
            "station_id": o.station_id,
            "date": str(o.obs_date),
            "precip_mm": o.precip_mm if o.precip_mm is not None else "",
            "temp_max_c": o.temp_max_c if o.temp_max_c is not None else "",
            "temp_min_c": o.temp_min_c if o.temp_min_c is not None else "",
            "temp_mean_c": o.temp_mean_c if o.temp_mean_c is not None else "",
            "humidity_pct": o.humidity_pct if o.humidity_pct is not None else "",
            "wind_speed_ms": o.wind_speed_ms if o.wind_speed_ms is not None else "",
            "pressure_hpa": o.pressure_hpa if o.pressure_hpa is not None else "",
            "source": o.source,
            "is_provisional": "1" if o.is_provisional else "0",
        })

    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=station_{sid}.csv"},
    )


@router.get("/map/layers/stations", response_class=JSONResponse)
async def map_layer_stations(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    stations_r = await db.execute(
        select(Station).where(Station.is_active == True)  # noqa: E712
    )
    stations = stations_r.scalars().all()

    # Latest obs per station
    latest_obs: dict[str, StationObservation] = {}
    if stations:
        sids = [s.station_id for s in stations]
        latest_r = await db.execute(
            select(StationObservation)
            .where(StationObservation.station_id.in_(sids))
            .order_by(StationObservation.station_id, desc(StationObservation.obs_date))
        )
        seen: set[str] = set()
        for obs in latest_r.scalars().all():
            if obs.station_id not in seen:
                latest_obs[obs.station_id] = obs
                seen.add(obs.station_id)

    features = []
    for s in stations:
        obs = latest_obs.get(s.station_id)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [s.lon, s.lat]},
            "properties": {
                "station_id": s.station_id,
                "name": s.name,
                "country": s.country,
                "elevation_m": s.elevation_m,
                "latest_date": str(obs.obs_date) if obs else None,
                "precip_mm": obs.precip_mm if obs else None,
                "temp_max_c": obs.temp_max_c if obs else None,
                "temp_min_c": obs.temp_min_c if obs else None,
                "is_provisional": obs.is_provisional if obs else None,
            },
        })

    return {"type": "FeatureCollection", "features": features}
