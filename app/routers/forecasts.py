import json
import re
import tempfile
import os

import httpx
import numpy as np
import xarray as xr
from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.forecast import ForecastUpload
from app.routers.triggers import evaluate_triggers

PORTAL_BASE = "https://open-data.rimes.int"

COUNTRY_NAMES = {
    "ae": "United Arab Emirates", "af": "Afghanistan",  "ao": "Angola",
    "bd": "Bangladesh",           "bf": "Burkina Faso", "bt": "Bhutan",
    "bw": "Botswana",             "cd": "DR Congo",     "cg": "Congo",
    "cm": "Cameroon",             "dj": "Djibouti",     "fj": "Fiji",
    "in": "India",                "jm": "Jamaica",      "ke": "Kenya",
    "kh": "Cambodia",             "km": "Comoros",      "la": "Laos",
    "lk": "Sri Lanka",            "ls": "Lesotho",      "mg": "Madagascar",
    "mm": "Myanmar",              "mn": "Mongolia",     "mu": "Mauritius",
    "mv": "Maldives",             "mw": "Malawi",       "mz": "Mozambique",
    "na": "Namibia",              "ng": "Nigeria",      "np": "Nepal",
    "pg": "Papua New Guinea",     "ph": "Philippines",  "pk": "Pakistan",
    "sc": "Seychelles",           "so": "Somalia",      "sz": "Eswatini",
    "td": "Chad",                 "th": "Thailand",     "tl": "Timor-Leste",
    "to": "Tonga",                "tz": "Tanzania",     "ws": "Samoa",
    "ye": "Yemen",                "za": "South Africa", "zm": "Zambia",
    "zw": "Zimbabwe",
}

SOURCES = [
    {"value": "regional_rimes", "label": "Regional — RIMES",          "path": "Regional/rimes/ECMWF/ifs15"},
    {"value": "regional_sea",   "label": "Regional — South-East Asia", "path": "Regional/sea/ECMWF/ifs15"},
] + [
    {"value": f"country_{cc}", "label": f"{name} ({cc.upper()})", "path": f"Countries/{cc}/ECMWF/ifs15"}
    for cc, name in sorted(COUNTRY_NAMES.items(), key=lambda x: x[1])
]
_SOURCE_MAP = {s["value"]: s for s in SOURCES}


async def _fetch_portal_dates() -> list[str]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{PORTAL_BASE}/Regional/rimes/ECMWF/ifs15/", timeout=10)
        resp.raise_for_status()
    return sorted(re.findall(r'href="(\d{8})/"', resp.text), reverse=True)

router = APIRouter(prefix="/forecasts")
templates = Jinja2Templates(directory="app/templates")

PRECIP_VARS = ["precipitation", "precip", "pr", "tp", "rain", "rainfall"]


def _find_precip_var(ds: xr.Dataset) -> str:
    for name in PRECIP_VARS:
        if name in ds:
            return name
    # fall back to first data variable
    return list(ds.data_vars)[0]


def _find_coord(ds: xr.Dataset, candidates: list[str]) -> str | None:
    for name in candidates:
        if name in ds.coords or name in ds.dims:
            return name
    return None


def _build_geojson(lats: np.ndarray, lons: np.ndarray, values: np.ndarray) -> str:
    """Convert a 2-D precipitation grid to a GeoJSON FeatureCollection of cell polygons."""
    dlat = float(abs(lats[1] - lats[0])) / 2 if len(lats) > 1 else 0.25
    dlon = float(abs(lons[1] - lons[0])) / 2 if len(lons) > 1 else 0.25

    vmin = float(np.nanmin(values))
    vmax = float(np.nanmax(values))
    vrange = vmax - vmin if vmax != vmin else 1.0

    features = []
    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            val = float(values[i, j])
            if np.isnan(val):
                continue
            intensity = (val - vmin) / vrange  # 0-1
            features.append({
                "type": "Feature",
                "properties": {"precip": round(val, 3), "intensity": round(intensity, 3)},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [lon - dlon, lat - dlat],
                        [lon + dlon, lat - dlat],
                        [lon + dlon, lat + dlat],
                        [lon - dlon, lat + dlat],
                        [lon - dlon, lat - dlat],
                    ]],
                },
            })
    return json.dumps({"type": "FeatureCollection", "features": features})


def _process_netcdf(path: str) -> dict:
    try:
        ds = xr.open_dataset(path, engine="h5netcdf")
    except Exception:
        ds = xr.open_dataset(path, engine="scipy")

    var_name = _find_precip_var(ds)
    lat_name = _find_coord(ds, ["lat", "latitude", "y"])
    lon_name = _find_coord(ds, ["lon", "longitude", "x"])
    time_name = _find_coord(ds, ["time", "t"])

    if lat_name is None or lon_name is None:
        raise ValueError("Could not find latitude/longitude coordinates in the NetCDF file.")

    da = ds[var_name]

    # Collapse time dimension by taking the mean if present
    if time_name and time_name in da.dims:
        times = ds[time_name].values
        time_start = str(times[0])[:19]
        time_end = str(times[-1])[:19]
        time_steps = int(len(times))
        da_mean = da.mean(dim=time_name)
    else:
        time_start = time_end = "N/A"
        time_steps = 1
        da_mean = da

    # Ensure lat/lon are the final two dims
    da_mean = da_mean.squeeze()

    lats = ds[lat_name].values.flatten()
    lons = ds[lon_name].values.flatten()

    # Subsample large grids to keep GeoJSON manageable (max 100x100)
    max_cells = 100
    lat_step = max(1, len(lats) // max_cells)
    lon_step = max(1, len(lons) // max_cells)
    lats_s = lats[::lat_step]
    lons_s = lons[::lon_step]

    values = da_mean.values
    if values.ndim == 2:
        values_s = values[::lat_step, ::lon_step]
    else:
        values_s = values.reshape(len(lats), len(lons))[::lat_step, ::lon_step]

    geojson = _build_geojson(lats_s, lons_s, values_s)

    flat = values.flatten().astype(float)
    flat = flat[~np.isnan(flat)]

    ds.close()

    return {
        "lat_min": float(lats.min()),
        "lat_max": float(lats.max()),
        "lon_min": float(lons.min()),
        "lon_max": float(lons.max()),
        "time_start": time_start,
        "time_end": time_end,
        "time_steps": time_steps,
        "precip_min": round(float(flat.min()), 3) if len(flat) else 0.0,
        "precip_max": round(float(flat.max()), 3) if len(flat) else 0.0,
        "precip_mean": round(float(flat.mean()), 3) if len(flat) else 0.0,
        "geojson": geojson,
    }


@router.get("", response_class=HTMLResponse)
async def forecast_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(
        select(ForecastUpload).order_by(desc(ForecastUpload.uploaded_at))
    )
    forecasts = result.scalars().all()

    return templates.TemplateResponse(
        "forecast_list.html", {"request": request, "user": user, "forecasts": forecasts}
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("forecast_upload.html", {"request": request, "user": user})


@router.post("/upload", response_class=HTMLResponse)
async def upload_forecast(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    if not file.filename.endswith(".nc"):
        return templates.TemplateResponse(
            "forecast_upload.html",
            {"request": request, "user": user, "error": "Only .nc (NetCDF) files are supported."},
        )

    contents = await file.read()

    try:
        with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name

        stats = _process_netcdf(tmp_path)
    except Exception as exc:
        return templates.TemplateResponse(
            "forecast_upload.html",
            {"request": request, "user": user, "error": f"Failed to process file: {exc}"},
        )
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    forecast = ForecastUpload(filename=file.filename, **stats)
    db.add(forecast)
    await db.commit()
    await db.refresh(forecast)

    await evaluate_triggers(forecast, db)

    return RedirectResponse(f"/forecasts/{forecast.id}", status_code=303)


@router.get("/import", response_class=HTMLResponse)
async def import_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    try:
        dates = await _fetch_portal_dates()
        portal_error = None
    except Exception as exc:
        dates = []
        portal_error = f"Could not reach RIMES portal: {exc}"

    return templates.TemplateResponse(
        "forecast_import.html",
        {"request": request, "user": user, "sources": SOURCES,
         "dates": dates, "portal_error": portal_error, "portal_base": PORTAL_BASE},
    )


@router.post("/import")
async def import_forecast(
    request: Request,
    source: str = Form(...),
    date: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    source_entry = _SOURCE_MAP.get(source)
    if not source_entry or not re.fullmatch(r"\d{8}", date):
        return RedirectResponse("/forecasts/import")

    url = f"{PORTAL_BASE}/{source_entry['path']}/{date}/tp.nc"
    source_key = source.replace("regional_", "").replace("country_", "")
    filename = f"ecmwf_tp_{source_key}_{date}.nc"

    tmp_path = None
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url, timeout=120) as resp:
                if resp.status_code == 404:
                    raise ValueError(f"No data available for {date} / {source_entry['label']}")
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as tmp:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        tmp.write(chunk)
                    tmp_path = tmp.name

        stats = _process_netcdf(tmp_path)
    except Exception as exc:
        try:
            dates = await _fetch_portal_dates()
        except Exception:
            dates = []
        return templates.TemplateResponse(
            "forecast_import.html",
            {"request": request, "user": user, "sources": SOURCES,
             "dates": dates, "portal_error": None,
             "error": f"Import failed: {exc}", "portal_base": PORTAL_BASE},
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    forecast = ForecastUpload(filename=filename, **stats)
    db.add(forecast)
    await db.commit()
    await db.refresh(forecast)
    await evaluate_triggers(forecast, db)
    return RedirectResponse(f"/forecasts/{forecast.id}", status_code=303)


@router.get("/{forecast_id}", response_class=HTMLResponse)
async def forecast_detail(forecast_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(select(ForecastUpload).where(ForecastUpload.id == forecast_id))
    forecast = result.scalar_one_or_none()
    if not forecast:
        return RedirectResponse("/dashboard")

    return templates.TemplateResponse(
        "forecast_detail.html",
        {"request": request, "user": user, "forecast": forecast},
    )


async def get_recent_forecasts(db: AsyncSession, limit: int = 5):
    result = await db.execute(
        select(ForecastUpload).order_by(desc(ForecastUpload.uploaded_at)).limit(limit)
    )
    return result.scalars().all()
