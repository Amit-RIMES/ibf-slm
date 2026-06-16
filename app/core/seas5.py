"""
SEAS5 seasonal forecast ingestion via Copernicus Data Store (CDS).

Fetches ECMWF SEAS5 monthly ensemble mean precipitation for the configured
region and stores records in the SeasonalForecast table.

Requires: cdsapi (pip install cdsapi)
CDS account: https://cds.climate.copernicus.eu (free registration)
"""
import json
import logging
import os
import tempfile
import zipfile
from datetime import date, datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _maybe_unzip(path: str) -> str:
    """If path is a zip, extract the first .nc file next to it and return its path."""
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic[:2] != b"PK":
        return path
    with zipfile.ZipFile(path) as zf:
        nc_names = [n for n in zf.namelist() if n.endswith(".nc")]
        if not nc_names:
            return path
        dest = path.replace(".nc", "_extracted.nc")
        with zf.open(nc_names[0]) as src, open(dest, "wb") as dst:
            dst.write(src.read())
    return dest

# Lead months to request (1 = next calendar month, up to 6)
_LEAD_MONTHS = ["1", "2", "3", "4", "5", "6"]


def _cdsapi_client(api_url: str, api_key: str):
    try:
        import cdsapi
        return cdsapi.Client(url=api_url, key=api_key, quiet=True, verify=True)
    except ImportError:
        logger.error("cdsapi not installed. Install with: pip install cdsapi")
        raise


def _seas5_to_seasonal_records(
    nc_path: str,
    issue_date: date,
    lead_months: int,
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
) -> list[dict]:
    """Parse SEAS5 NetCDF and return list of SeasonalForecast-compatible dicts."""
    import numpy as np
    import xarray as xr

    try:
        ds = xr.open_dataset(nc_path)
    except Exception as exc:
        logger.error("SEAS5: failed to open NetCDF: %s", exc)
        return []

    tp_name = next((v for v in ("tprate", "tp", "mtpr") if v in ds.data_vars), None)
    if tp_name is None:
        tp_name = list(ds.data_vars)[0]

    lat_name = next((c for c in ("latitude", "lat") if c in ds.coords), None)
    lon_name = next((c for c in ("longitude", "lon") if c in ds.coords), None)
    time_name = next((c for c in ("time", "valid_time", "forecast_reference_time") if c in ds.coords), None)

    if lat_name is None or lon_name is None:
        logger.error("SEAS5: cannot find lat/lon in dataset. Coords: %s", list(ds.coords))
        ds.close()
        return []

    da = ds[tp_name]
    lats = ds[lat_name].values
    lons = ds[lon_name].values

    # Subset to bbox
    lat_mask = (lats >= lat_min) & (lats <= lat_max)
    lon_mask = (lons >= lon_min) & (lons <= lon_max)

    da_sub = da.isel({lat_name: lat_mask, lon_name: lon_mask})

    records = []
    import calendar

    # Handle time dimension — each step is one valid month
    if time_name and time_name in da_sub.dims:
        time_vals = ds[time_name].values
        n_steps = min(len(time_vals), lead_months)
    else:
        # Single time step
        time_vals = [None]
        n_steps = 1

    for i in range(n_steps):
        try:
            if time_vals[i] is not None:
                # Convert numpy datetime64 → python date
                valid_dt = datetime.utcfromtimestamp(
                    int(time_vals[i]) / 1e9
                ).date() if hasattr(time_vals[i], "__int__") else (
                    da_sub.coords[time_name].values[i].astype("datetime64[s]").astype(datetime).date()
                )
            else:
                # Estimate from issue_date + lead month
                y = issue_date.year + (issue_date.month + i) // 12
                m = (issue_date.month + i) % 12 or 12
                valid_dt = date(y, m, 1)

            valid_start = date(valid_dt.year, valid_dt.month, 1)
            last_day = calendar.monthrange(valid_dt.year, valid_dt.month)[1]
            valid_end = date(valid_dt.year, valid_dt.month, last_day)

            if time_name and time_name in da_sub.dims:
                da_step = da_sub.isel({time_name: i})
            else:
                da_step = da_sub

            # Collapse remaining dims (forecast_reference_time, etc.)
            for dim in list(da_step.dims):
                if dim not in (lat_name, lon_name):
                    da_step = da_step.mean(dim=dim)

            vals = da_step.values.flatten().astype(float)
            vals = vals[~np.isnan(vals)]

            if not len(vals):
                continue

            # SEAS5 tp is in m/s (rate) — convert to mm/month
            # Monthly mean rate (m/s) × seconds_per_month × 1000 = mm/month
            seconds_per_month = last_day * 86400
            regional_mean_mm = float(vals.mean()) * seconds_per_month * 1000.0

            # Approximate tercile probabilities from ensemble spread
            # (Using climatological average of ~80 mm/month for tropics as rough reference)
            # This is a heuristic; proper tercile probs require a hindcast baseline
            clim_approx = 80.0  # mm/month tropical approximate
            anomaly_pct = ((regional_mean_mm - clim_approx) / clim_approx) * 100.0 if clim_approx > 0 else 0.0
            anomaly_pct = round(anomaly_pct, 1)

            if anomaly_pct > 15:
                above, near, below = 55, 30, 15
            elif anomaly_pct > 5:
                above, near, below = 45, 35, 20
            elif anomaly_pct < -15:
                above, near, below = 15, 30, 55
            elif anomaly_pct < -5:
                above, near, below = 20, 35, 45
            else:
                above, near, below = 33, 34, 33

            records.append({
                "source": "SEAS5",
                "issue_date": issue_date,
                "valid_start": valid_start,
                "valid_end": valid_end,
                "variable": "precip",
                "below_normal_pct": float(below),
                "near_normal_pct": float(near),
                "above_normal_pct": float(above),
                "precip_anomaly_pct": anomaly_pct,
                "region_label": f"Lat {lat_min}–{lat_max}, Lon {lon_min}–{lon_max}",
                "lat_min": lat_min, "lat_max": lat_max,
                "lon_min": lon_min, "lon_max": lon_max,
                "notes": (
                    f"SEAS5 ensemble mean: {regional_mean_mm:.1f} mm/month. "
                    "Tercile probs are approximate (heuristic, not from hindcast climatology)."
                ),
            })
        except Exception as exc:
            logger.warning("SEAS5: failed to process lead month %d: %s", i + 1, exc)
            continue

    ds.close()
    return records


async def fetch_seas5(
    api_url: str,
    api_key: str,
    lat_min: float = 0.0,
    lat_max: float = 35.0,
    lon_min: float = 60.0,
    lon_max: float = 155.0,
    lead_months: int = 6,
    issue_date: Optional[date] = None,
) -> list[dict]:
    """
    Fetch SEAS5 monthly precipitation forecasts from CDS.

    Returns a list of SeasonalForecast-compatible dicts (one per forecast month),
    or an empty list on failure.
    """
    if not api_key:
        logger.error("SEAS5: no CDS API key configured")
        return []

    if issue_date is None:
        # SEAS5 is issued on the 1st of each month with ~2 week lag.
        # Use previous month to ensure the forecast has been published.
        today = datetime.now(timezone.utc).date()
        if today.month == 1:
            issue_date = today.replace(year=today.year - 1, month=12, day=1)
        else:
            issue_date = today.replace(month=today.month - 1, day=1)

    try:
        client = _cdsapi_client(api_url, api_key)
    except ImportError:
        return []

    with tempfile.TemporaryDirectory() as tmpdir:
        output = os.path.join(tmpdir, "seas5.nc")
        request_params = {
            "originating_centre": "ecmwf",
            "system": "51",  # SEAS5.1 — operational from 2022, system 5 (original) has no recent data
            "variable": "total_precipitation",
            "product_type": "monthly_mean",
            "year": str(issue_date.year),
            "month": str(issue_date.month).zfill(2),
            "leadtime_month": _LEAD_MONTHS[:lead_months],
            "area": [lat_max, lon_min, lat_min, lon_max],
            "data_format": "netcdf",
        }
        logger.info(
            "SEAS5: requesting issue=%s lead_months=%d area=[%.1f,%.1f,%.1f,%.1f]",
            issue_date, lead_months, lat_max, lon_min, lat_min, lon_max,
        )
        try:
            import asyncio
            await asyncio.to_thread(
                client.retrieve, "seasonal-monthly-single-levels", request_params, output
            )
        except Exception as exc:
            logger.error("SEAS5 CDS retrieve failed: %s", exc)
            return []

        if not os.path.exists(output) or os.path.getsize(output) == 0:
            logger.error("SEAS5: downloaded file missing or empty")
            return []

        logger.info("SEAS5: downloaded %.1f KB, parsing...", os.path.getsize(output) / 1024)
        nc_path = _maybe_unzip(output)
        return _seas5_to_seasonal_records(nc_path, issue_date, lead_months, lat_min, lat_max, lon_min, lon_max)
