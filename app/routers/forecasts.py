import json
import tempfile
import os

import numpy as np
import xarray as xr
from fastapi import APIRouter, Depends, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.forecast import ForecastUpload
from app.models.user import User

router = APIRouter(prefix="/forecasts")
templates = Jinja2Templates(directory="app/templates")

PRECIP_VARS = ["precipitation", "precip", "pr", "tp", "rain", "rainfall"]


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    result = await db.execute(select(User).where(User.id == int(payload["sub"])))
    return result.scalar_one_or_none()


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
