"""
ERA5 reanalysis ingestion via Copernicus Data Store (CDS).

Fetches ERA5-Land daily total precipitation for a date range and
stores records in the ObservedRainfall table (source="ERA5").

ERA5-Land covers 1950-present at 0.1° resolution.
ERA5 (full) covers 1940-present at 0.25° resolution.

Requires: cdsapi (pip install cdsapi)
"""
import logging
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


def _cdsapi_client(api_url: str, api_key: str):
    try:
        import cdsapi
        return cdsapi.Client(url=api_url, key=api_key, quiet=True, verify=True)
    except ImportError:
        logger.error("cdsapi not installed. Install with: pip install cdsapi")
        raise


def _process_era5_nc(
    nc_path: str,
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
) -> list[dict]:
    """Parse ERA5 NetCDF (daily precip) → list of ObservedRainfall-compatible dicts."""
    import json
    import xarray as xr

    try:
        ds = xr.open_dataset(nc_path)
    except Exception as exc:
        logger.error("ERA5: failed to open NetCDF: %s", exc)
        return []

    tp_name = next((v for v in ("tp", "total_precipitation") if v in ds.data_vars), None)
    if tp_name is None:
        tp_name = list(ds.data_vars)[0]

    lat_name = next((c for c in ("latitude", "lat") if c in ds.coords), None)
    lon_name = next((c for c in ("longitude", "lon") if c in ds.coords), None)
    time_name = next((c for c in ("time", "valid_time") if c in ds.coords), None)

    if lat_name is None or lon_name is None or time_name is None:
        logger.error("ERA5: cannot find lat/lon/time in dataset. Coords: %s", list(ds.coords))
        ds.close()
        return []

    da = ds[tp_name]
    lats = ds[lat_name].values
    lons = ds[lon_name].values
    times = ds[time_name].values

    # Ensure lats ascending
    if len(lats) > 1 and lats[0] > lats[-1]:
        lats = lats[::-1]
        da = da.isel({lat_name: slice(None, None, -1)})

    # Subset to bbox
    lat_mask = (lats >= lat_min) & (lats <= lat_max)
    lon_mask = (lons >= lon_min) & (lons <= lon_max)

    da_sub = da.isel({lat_name: lat_mask, lon_name: lon_mask})
    lats_s = lats[lat_mask]
    lons_s = lons[lon_mask]

    records = []
    for t_idx, t_val in enumerate(times):
        try:
            obs_date = datetime.utcfromtimestamp(
                int(t_val) / 1e9
            ).date() if hasattr(t_val, "__int__") else (
                np.datetime64(t_val, "s").astype(datetime).date()
            )
        except Exception:
            try:
                obs_date = str(t_val)[:10]
                obs_date = date.fromisoformat(obs_date)
            except Exception:
                continue

        try:
            if time_name in da_sub.dims:
                day_vals = da_sub.isel({time_name: t_idx}).values.flatten().astype(float)
            else:
                day_vals = da_sub.values.flatten().astype(float)
        except Exception as exc:
            logger.warning("ERA5: failed to extract day %s: %s", obs_date, exc)
            continue

        day_vals = day_vals[~np.isnan(day_vals)]
        if not len(day_vals):
            continue

        # ERA5-Land tp is m/day — convert to mm/day
        day_vals_mm = day_vals * 1000.0
        day_vals_mm = np.clip(day_vals_mm, 0, None)

        # Build simple GeoJSON (same style as CHIRPS)
        try:
            geojson = _build_era5_geojson(
                lats_s, lons_s,
                da_sub.isel({time_name: t_idx}).values if time_name in da_sub.dims else da_sub.values,
            )
        except Exception:
            geojson = None

        records.append({
            "obs_date": obs_date,
            "source": "ERA5",
            "lat_min": float(lats_s.min()),
            "lat_max": float(lats_s.max()),
            "lon_min": float(lons_s.min()),
            "lon_max": float(lons_s.max()),
            "precip_mean": round(float(day_vals_mm.mean()), 3),
            "precip_max": round(float(day_vals_mm.max()), 3),
            "precip_min": round(float(day_vals_mm.min()), 3),
            "wet_fraction": round(float((day_vals_mm > 1.0).mean()), 4),
            "pixel_count": int(len(day_vals_mm)),
            "is_preliminary": False,
            "geojson": geojson,
        })

    ds.close()
    return records


def _build_era5_geojson(lats: np.ndarray, lons: np.ndarray, values_2d: np.ndarray) -> Optional[str]:
    import json
    vals = values_2d * 1000.0  # m → mm
    vmin, vmax = float(np.nanmin(vals)), float(np.nanmax(vals))
    vrange = vmax - vmin if vmax != vmin else 1.0
    dlat = float(abs(lats[1] - lats[0])) / 2 if len(lats) > 1 else 0.1
    dlon = float(abs(lons[1] - lons[0])) / 2 if len(lons) > 1 else 0.1
    max_cells = 80
    lat_step = max(1, len(lats) // max_cells)
    lon_step = max(1, len(lons) // max_cells)
    features = []
    for i in range(0, len(lats), lat_step):
        for j in range(0, len(lons), lon_step):
            if values_2d.ndim == 2:
                val = float(values_2d[i, j]) * 1000.0
            else:
                continue
            if np.isnan(val) or val < 0.1:
                continue
            lat, lon = float(lats[i]), float(lons[j])
            intensity = (val - vmin) / vrange
            features.append({
                "type": "Feature",
                "properties": {"precip": round(val, 2), "intensity": round(intensity, 3)},
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


async def fetch_era5(
    api_url: str,
    api_key: str,
    lat_min: float = 0.0,
    lat_max: float = 35.0,
    lon_min: float = 60.0,
    lon_max: float = 155.0,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    lookback_days: int = 30,
) -> list[dict]:
    """
    Fetch ERA5-Land daily precipitation from CDS.

    Returns list of ObservedRainfall-compatible dicts, or empty list on failure.
    """
    if not api_key:
        logger.error("ERA5: no CDS API key configured")
        return []

    today = datetime.now(timezone.utc).date()
    if end_date is None:
        # ERA5 has ~5 day latency
        end_date = today - timedelta(days=5)
    if start_date is None:
        start_date = end_date - timedelta(days=lookback_days - 1)

    if start_date > end_date:
        logger.warning("ERA5: start_date > end_date, skipping")
        return []

    # Build list of months covered by the date range
    months_needed: set[tuple[int, int]] = set()
    d = start_date
    while d <= end_date:
        months_needed.add((d.year, d.month))
        d = d.replace(day=28) + timedelta(days=4)
        d = d.replace(day=1)

    all_records: list[dict] = []

    try:
        client = _cdsapi_client(api_url, api_key)
    except ImportError:
        return []

    for (year, month) in sorted(months_needed):
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        days = [str(d).zfill(2) for d in range(1, last_day + 1)]

        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, f"era5_{year}{month:02d}.nc")
            request_params = {
                "variable": "total_precipitation",
                "year": str(year),
                "month": str(month).zfill(2),
                "day": days,
                "time": "00:00",
                "area": [lat_max, lon_min, lat_min, lon_max],
                "format": "netcdf",
            }
            logger.info("ERA5: requesting %d-%02d area=[%.1f,%.1f,%.1f,%.1f]",
                        year, month, lat_max, lon_min, lat_min, lon_max)
            try:
                client.retrieve("reanalysis-era5-land", request_params, output)
            except Exception as exc:
                logger.error("ERA5 CDS retrieve failed for %d-%02d: %s", year, month, exc)
                continue

            if not os.path.exists(output) or os.path.getsize(output) == 0:
                continue

            logger.info("ERA5: downloaded %.1f KB for %d-%02d", os.path.getsize(output) / 1024, year, month)
            month_records = _process_era5_nc(output, lat_min, lat_max, lon_min, lon_max)
            # Filter to requested date range
            filtered = [r for r in month_records if start_date <= r["obs_date"] <= end_date]
            all_records.extend(filtered)

    return all_records
