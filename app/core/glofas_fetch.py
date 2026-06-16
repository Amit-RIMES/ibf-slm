"""
GloFAS (Global Flood Awareness System) river discharge forecast ingestion
via Copernicus Data Store (CDS).

Fetches GloFAS ensemble mean river discharge forecasts for the configured
region and stores records in the GlofasRecord table.

Dataset: cems-glofas-forecast
Variable: river_discharge_in_the_last_24_hours (m³/s)

Requires: cdsapi (pip install cdsapi)
"""
import json
import logging
import os
import tempfile
from datetime import date, datetime, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Lead times to request (hours)
_LEAD_HOURS = [str(h) for h in range(24, 241, 24)]  # 24h–240h (10 days)


def _cdsapi_client(api_url: str, api_key: str):
    try:
        import cdsapi
        return cdsapi.Client(url=api_url, key=api_key, quiet=True, verify=True)
    except ImportError:
        logger.error("cdsapi not installed. Install with: pip install cdsapi")
        raise


def _build_discharge_geojson(
    lats: np.ndarray,
    lons: np.ndarray,
    values_2d: np.ndarray,
    threshold: float = 1.0,
) -> str:
    """Build GeoJSON showing only river network cells (discharge > threshold m³/s)."""
    vmin, vmax = float(np.nanmin(values_2d)), float(np.nanmax(values_2d))
    vrange = max(vmax - threshold, 1.0)
    dlat = float(abs(lats[1] - lats[0])) / 2 if len(lats) > 1 else 0.1
    dlon = float(abs(lons[1] - lons[0])) / 2 if len(lons) > 1 else 0.1

    max_cells = 5000
    total_river_cells = int(np.sum(~np.isnan(values_2d) & (values_2d > threshold)))
    step = max(1, total_river_cells // max_cells)

    features = []
    count = 0
    for i in range(len(lats)):
        for j in range(len(lons)):
            val = float(values_2d[i, j])
            if np.isnan(val) or val <= threshold:
                continue
            count += 1
            if count % step != 1:
                continue
            lat, lon = float(lats[i]), float(lons[j])
            intensity = min(1.0, (val - threshold) / vrange)
            features.append({
                "type": "Feature",
                "properties": {
                    "discharge": round(val, 1),
                    "intensity": round(intensity, 3),
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [lon - dlon, lat - dlat], [lon + dlon, lat - dlat],
                        [lon + dlon, lat + dlat], [lon - dlon, lat + dlat],
                        [lon - dlon, lat - dlat],
                    ]],
                },
            })

    return json.dumps({"type": "FeatureCollection", "features": features})


def _process_glofas_nc(
    nc_path: str,
    forecast_date: date,
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
) -> Optional[dict]:
    """Parse GloFAS NetCDF → GlofasRecord-compatible dict."""
    import xarray as xr

    try:
        ds = xr.open_dataset(nc_path)
    except Exception as exc:
        logger.error("GloFAS: failed to open NetCDF: %s", exc)
        return None

    # Variable name varies by GloFAS version
    dis_name = next(
        (v for v in ("dis24", "dis", "river_discharge_in_the_last_24_hours", "ro")
         if v in ds.data_vars),
        None,
    )
    if dis_name is None:
        dis_name = list(ds.data_vars)[0]

    lat_name = next((c for c in ("latitude", "lat") if c in ds.coords), None)
    lon_name = next((c for c in ("longitude", "lon") if c in ds.coords), None)

    if lat_name is None or lon_name is None:
        logger.error("GloFAS: cannot find lat/lon. Coords: %s", list(ds.coords))
        ds.close()
        return None

    da = ds[dis_name]
    lats = ds[lat_name].values
    lons = ds[lon_name].values

    # Ensure lats ascending
    if len(lats) > 1 and lats[0] > lats[-1]:
        lats = lats[::-1]
        da = da.isel({lat_name: slice(None, None, -1)})

    # Collapse non-spatial dims to get ensemble mean at final lead time
    for dim in list(da.dims):
        if dim not in (lat_name, lon_name):
            da = da.mean(dim=dim)

    # Subset to bbox
    lat_mask = (lats >= lat_min) & (lats <= lat_max)
    lon_mask = (lons >= lon_min) & (lons <= lon_max)
    sub = da.isel({lat_name: lat_mask, lon_name: lon_mask})
    lats_s = lats[lat_mask]
    lons_s = lons[lon_mask]

    vals = sub.values
    flat = vals.flatten().astype(float)
    flat_valid = flat[~np.isnan(flat) & (flat >= 0)]

    if not len(flat_valid):
        logger.error("GloFAS: no valid discharge values in bbox")
        ds.close()
        return None

    geojson = _build_discharge_geojson(lats_s, lons_s, vals)
    ds.close()

    return {
        "forecast_date": forecast_date,
        "source": "GloFAS-v4",
        "uploaded_at": datetime.now(timezone.utc),
        "lat_min": round(float(lats_s.min()), 4),
        "lat_max": round(float(lats_s.max()), 4),
        "lon_min": round(float(lons_s.min()), 4),
        "lon_max": round(float(lons_s.max()), 4),
        "discharge_min": round(float(flat_valid.min()), 2),
        "discharge_max": round(float(flat_valid.max()), 2),
        "discharge_mean": round(float(flat_valid.mean()), 2),
        "lead_days": 10,
        "geojson": geojson,
    }


async def fetch_glofas(
    api_url: str,
    api_key: str,
    lat_min: float = 0.0,
    lat_max: float = 35.0,
    lon_min: float = 60.0,
    lon_max: float = 155.0,
    forecast_date: Optional[date] = None,
) -> Optional[dict]:
    """
    Fetch GloFAS 10-day river discharge forecast from CDS.

    Returns a GlofasRecord-compatible dict, or None on failure.
    """
    if not api_key:
        logger.error("GloFAS: no CDS API key configured")
        return None

    if forecast_date is None:
        forecast_date = datetime.now(timezone.utc).date()

    try:
        client = _cdsapi_client(api_url, api_key)
    except ImportError:
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        output = os.path.join(tmpdir, "glofas.nc")
        request_params = {
            "system_version": "operational",
            "hydrological_model": "lisflood",
            "product_type": "ensemble_perturbed_forecasts",
            "variable": "river_discharge_in_the_last_24_hours",
            "year": str(forecast_date.year),
            "month": str(forecast_date.month).zfill(2),
            "day": str(forecast_date.day).zfill(2),
            "leadtime_hour": _LEAD_HOURS,
            "area": [lat_max, lon_min, lat_min, lon_max],
            "format": "netcdf",
        }
        logger.info(
            "GloFAS: requesting date=%s area=[%.1f,%.1f,%.1f,%.1f]",
            forecast_date, lat_max, lon_min, lat_min, lon_max,
        )
        try:
            import asyncio
            await asyncio.to_thread(
                client.retrieve, "cems-glofas-forecast", request_params, output
            )
        except Exception as exc:
            logger.error("GloFAS CDS retrieve failed: %s", exc)
            return None

        if not os.path.exists(output) or os.path.getsize(output) == 0:
            logger.error("GloFAS: downloaded file missing or empty")
            return None

        logger.info("GloFAS: downloaded %.1f KB, parsing...", os.path.getsize(output) / 1024)
        return _process_glofas_nc(output, forecast_date, lat_min, lat_max, lon_min, lon_max)
