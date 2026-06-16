"""
ECMWF Open Data (IFS) forecast ingestion.

Downloads ECMWF IFS forecasts using the free ecmwf-opendata Python client.
No API key or registration required.

Supported variables:
  tp      = total precipitation accumulation (metres → mm)
  2t      = 2-metre temperature (Kelvin → °C)
  wind10  = 10-metre wind speed (computed from 10u + 10v, m/s)
  msl     = mean sea level pressure (Pascal → hPa)

HRES (deterministic): 0.25° resolution, 10-day horizon
ENS  (ensemble, 51 members): 0.5° resolution, 15-day horizon
"""
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_MM_PER_M = 1000.0  # ECMWF tp is in metres
_K_OFFSET = 273.15  # Kelvin → Celsius
_PA_PER_HPA = 100.0  # Pa → hPa

# Steps to request (hours from forecast start)
_HRES_STEPS = [24, 48, 72, 96, 120, 144, 168, 192, 216, 240]
_ENS_STEPS = [24, 120, 240]

# ECMWF param codes per variable
_PARAM_MAP = {
    "tp": "tp",
    "2t": "2t",
    "wind10": ["10u", "10v"],
    "msl": "msl",
}

# Units label per variable (for filename and ForecastUpload.variable)
_SOURCE_SUFFIX = {
    "tp": "tp",
    "2t": "2t",
    "wind10": "wind10",
    "msl": "msl",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _subset_2d(
    arr: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
):
    lat_mask = (lats >= lat_min) & (lats <= lat_max)
    lon_mask = (lons >= lon_min) & (lons <= lon_max)
    return arr[np.ix_(lat_mask, lon_mask)], lats[lat_mask], lons[lon_mask]


def _build_geojson(lats: np.ndarray, lons: np.ndarray, values: np.ndarray) -> str:
    dlat = float(abs(lats[1] - lats[0])) / 2 if len(lats) > 1 else 0.25
    dlon = float(abs(lons[1] - lons[0])) / 2 if len(lons) > 1 else 0.25
    vmin, vmax = float(np.nanmin(values)), float(np.nanmax(values))
    vrange = vmax - vmin if vmax != vmin else 1.0
    max_cells = 100
    lat_step = max(1, len(lats) // max_cells)
    lon_step = max(1, len(lons) // max_cells)
    features = []
    for i in range(0, len(lats), lat_step):
        for j in range(0, len(lons), lon_step):
            val = float(values[i, j])
            if np.isnan(val):
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


def _bucket_stats(arr: np.ndarray) -> dict:
    a = arr.flatten().astype(float)
    a = a[~np.isnan(a)]
    if not len(a):
        return {"min": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "min": round(float(a.min()), 2),
        "max": round(float(a.max()), 2),
        "mean": round(float(a.mean()), 2),
    }


# ── GRIB2 processing ─────────────────────────────────────────────────────────

def _open_grib(path: str):
    """Open a GRIB2 file as an xarray Dataset, handling multi-hypercube files."""
    import xarray as xr
    try:
        return xr.open_dataset(path, engine="cfgrib")
    except Exception:
        pass

    # cfgrib may refuse to merge multiple hypercubes (e.g. different steps);
    # fall back to open_datasets and concatenate along step.
    try:
        import cfgrib
        import xarray as xr
        datasets = cfgrib.open_datasets(path)
        if not datasets:
            raise RuntimeError("cfgrib opened 0 datasets")
        if len(datasets) == 1:
            return datasets[0]
        return xr.concat(datasets, dim="step")
    except Exception as exc:
        raise RuntimeError(f"Could not open GRIB2 file: {exc}") from exc


def _apply_unit_conversion(da, variable: str, lat_name: str):
    """Apply unit conversion for the given variable. Returns (da_converted, is_cumulative)."""
    if variable == "tp":
        return da * _MM_PER_M, True  # m → mm, cumulative
    elif variable == "2t":
        return da - _K_OFFSET, False  # K → °C, instantaneous
    elif variable == "msl":
        return da / _PA_PER_HPA, False  # Pa → hPa, instantaneous
    else:
        return da, False  # wind10 or unknown: already m/s, not cumulative


def _extract_wind10(ds) -> Optional["np.ndarray"]:
    """Compute 10m wind speed magnitude from u and v components in a multi-dataset."""
    import cfgrib
    import xarray as xr
    datasets = cfgrib.open_datasets(ds if isinstance(ds, str) else ds.encoding.get("source", ""))
    u_ds = next((d for d in datasets if "u10" in d.data_vars or "u" in d.data_vars), None)
    v_ds = next((d for d in datasets if "v10" in d.data_vars or "v" in d.data_vars), None)
    if u_ds is None or v_ds is None:
        return None
    u_name = "u10" if "u10" in u_ds.data_vars else "u"
    v_name = "v10" if "v10" in v_ds.data_vars else "v"
    return np.sqrt(u_ds[u_name].values ** 2 + v_ds[v_name].values ** 2)


def _process_grib(
    path: str,
    source_label: str,
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    is_ensemble: bool,
    variable: str = "tp",
) -> Optional[dict]:
    if variable == "wind10":
        return _process_wind10_grib(path, source_label, lat_min, lat_max, lon_min, lon_max, is_ensemble)

    try:
        ds = _open_grib(path)
    except Exception as exc:
        logger.error("ECMWF GRIB parse failed: %s", exc)
        return None

    # Detect coordinate names
    lat_name = next((n for n in ("latitude", "lat", "y") if n in ds.coords or n in ds.dims), None)
    lon_name = next((n for n in ("longitude", "lon", "x") if n in ds.coords or n in ds.dims), None)
    if lat_name is None or lon_name is None:
        logger.error("ECMWF: cannot find lat/lon in GRIB dataset. Coords: %s", list(ds.coords))
        ds.close()
        return None

    # Find the relevant data variable
    var_candidates = {
        "tp": ("tp", "precipitation", "rain"),
        "2t": ("t2m", "2t", "t"),
        "msl": ("msl", "prmsl"),
    }
    candidates = var_candidates.get(variable, (variable,))
    field_name = next((n for n in candidates if n in ds.data_vars), None)
    if field_name is None:
        field_name = list(ds.data_vars)[0]

    da_raw = ds[field_name]
    da, is_cumulative = _apply_unit_conversion(da_raw, variable, lat_name)

    lats = ds[lat_name].values.flatten()
    lons = ds[lon_name].values.flatten()

    # Ensure lats are ascending (ECMWF data is often N→S)
    if len(lats) > 1 and lats[0] > lats[-1]:
        lats = lats[::-1]
        da = da.isel({lat_name: slice(None, None, -1)})

    # ── Ensemble dimension ────────────────────────────────────────────────────
    ens_dim = next((n for n in ("number", "member", "realization") if n in da.dims), None)
    ensemble_stats: dict = {}
    if ens_dim and is_ensemble:
        from app.core.ensemble import percentiles_from_members
        n_members = int(da.sizes[ens_dim])
        step_dim = next((n for n in ("step", "valid_time") if n in da.dims), None)
        da_final = da.isel({step_dim: -1}) if step_dim else da
        other_dims = [d for d in da_final.dims if d != ens_dim]
        per_member = da_final.mean(dim=other_dims).values.flatten().astype(float)
        per_member = per_member[~np.isnan(per_member)].tolist()
        ensemble_stats = percentiles_from_members(per_member)
        ensemble_stats["ensemble_size"] = n_members
        da = da.mean(dim=ens_dim)

    # ── Step / time dimension ─────────────────────────────────────────────────
    step_dim = next((n for n in ("step", "valid_time") if n in da.dims), None)
    time_start = time_end = "N/A"
    time_steps = 1
    lead_time_stats = None

    if step_dim and step_dim in da.dims:
        step_vals = da[step_dim].values
        time_steps = int(len(step_vals))
        try:
            step_hours = [int(s / np.timedelta64(1, "h")) for s in step_vals]
            time_start = f"T+{step_hours[0]:03d}h"
            time_end = f"T+{step_hours[-1]:03d}h"

            idx_5d = min(range(len(step_hours)), key=lambda i: abs(step_hours[i] - 120))
            idx_10d = min(range(len(step_hours)), key=lambda i: abs(step_hours[i] - 240))
            lt = {}
            arr_5d = da.isel({step_dim: idx_5d}).values
            if is_cumulative:
                lt["d1_5"] = _bucket_stats(arr_5d)
                if idx_10d != idx_5d:
                    arr_10d = da.isel({step_dim: idx_10d}).values
                    arr_6_10 = np.where(arr_10d - arr_5d >= 0, arr_10d - arr_5d, 0)
                    lt["d6_10"] = _bucket_stats(arr_6_10)
            else:
                # Instantaneous: stats per window mean
                lt["d1_5"] = _bucket_stats(da.isel({step_dim: slice(0, idx_5d + 1)}).mean(step_dim).values)
                if idx_10d != idx_5d:
                    lt["d6_10"] = _bucket_stats(da.isel({step_dim: slice(idx_5d + 1, idx_10d + 1)}).mean(step_dim).values)
            lead_time_stats = json.dumps(lt)
        except Exception as exc:
            logger.warning("ECMWF lead-time stats failed: %s", exc)

        if is_cumulative:
            da_total = da.isel({step_dim: -1})
        else:
            da_total = da.mean(dim=step_dim)
    else:
        da_total = da.squeeze()

    values_2d = da_total.values
    if values_2d.ndim != 2:
        try:
            values_2d = values_2d.reshape(len(lats), len(lons))
        except ValueError:
            logger.error("ECMWF: cannot reshape values to (%d, %d)", len(lats), len(lons))
            ds.close()
            return None

    # ── Spatial subset ────────────────────────────────────────────────────────
    sub, lats_s, lons_s = _subset_2d(values_2d, lats, lons, lat_min, lat_max, lon_min, lon_max)
    if sub.size == 0:
        logger.warning(
            "ECMWF: no grid points in bbox [%.1f–%.1f, %.1f–%.1f]; using full domain",
            lat_min, lat_max, lon_min, lon_max,
        )
        sub, lats_s, lons_s = values_2d, lats, lons

    flat = sub.flatten().astype(float)
    flat = flat[~np.isnan(flat)]
    if not len(flat):
        logger.error("ECMWF: all-NaN values after spatial subset")
        ds.close()
        return None

    geojson = _build_geojson(lats_s, lons_s, sub)
    now = datetime.now(timezone.utc)
    suffix = "ens" if is_ensemble else "hres"
    var_suffix = _SOURCE_SUFFIX.get(variable, variable)
    filename = f"ecmwf_ifs_{suffix}_{var_suffix}_{now.strftime('%Y%m%d_%H%M%S')}.grib2"

    ds.close()

    return {
        "filename": filename,
        "source": source_label,
        "variable": variable,
        "uploaded_at": now,
        "lat_min": round(float(lats_s.min()), 4),
        "lat_max": round(float(lats_s.max()), 4),
        "lon_min": round(float(lons_s.min()), 4),
        "lon_max": round(float(lons_s.max()), 4),
        "time_start": time_start,
        "time_end": time_end,
        "time_steps": time_steps,
        "precip_min": round(float(flat.min()), 2),
        "precip_max": round(float(flat.max()), 2),
        "precip_mean": round(float(flat.mean()), 2),
        "geojson": geojson,
        "lead_time_stats": lead_time_stats,
        **ensemble_stats,
    }


def _process_wind10_grib(
    path: str,
    source_label: str,
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    is_ensemble: bool,
) -> Optional[dict]:
    """Process 10m wind speed from a GRIB containing 10u and 10v."""
    import cfgrib
    import xarray as xr

    try:
        datasets = cfgrib.open_datasets(path)
    except Exception as exc:
        logger.error("ECMWF wind10 GRIB parse failed: %s", exc)
        return None

    u_ds = next((d for d in datasets if any(v in d.data_vars for v in ("u10", "u"))), None)
    v_ds = next((d for d in datasets if any(v in d.data_vars for v in ("v10", "v"))), None)

    if u_ds is None or v_ds is None:
        logger.error("ECMWF wind10: could not find u/v datasets in GRIB")
        return None

    u_name = "u10" if "u10" in u_ds.data_vars else "u"
    v_name = "v10" if "v10" in v_ds.data_vars else "v"

    lat_name = next((n for n in ("latitude", "lat") if n in u_ds.coords), None)
    lon_name = next((n for n in ("longitude", "lon") if n in u_ds.coords), None)
    if lat_name is None:
        logger.error("ECMWF wind10: cannot find lat/lon")
        return None

    u_da = u_ds[u_name]
    v_da = v_ds[v_name]

    # Average over ensemble and time
    for dim in list(u_da.dims):
        if dim not in (lat_name, lon_name):
            u_da = u_da.mean(dim=dim)
    for dim in list(v_da.dims):
        if dim not in (lat_name, lon_name):
            v_da = v_da.mean(dim=dim)

    try:
        speed_vals = np.sqrt(u_da.values ** 2 + v_da.values ** 2)
    except Exception as exc:
        logger.error("ECMWF wind10: magnitude computation failed: %s", exc)
        return None

    lats = u_ds[lat_name].values.flatten()
    lons = u_ds[lon_name].values.flatten()

    if len(lats) > 1 and lats[0] > lats[-1]:
        lats = lats[::-1]
        speed_vals = speed_vals[::-1, :]

    sub, lats_s, lons_s = _subset_2d(speed_vals, lats, lons, lat_min, lat_max, lon_min, lon_max)
    if sub.size == 0:
        sub, lats_s, lons_s = speed_vals, lats, lons

    flat = sub.flatten().astype(float)
    flat = flat[~np.isnan(flat)]
    if not len(flat):
        return None

    geojson = _build_geojson(lats_s, lons_s, sub)
    now = datetime.now(timezone.utc)
    suffix = "ens" if is_ensemble else "hres"
    filename = f"ecmwf_ifs_{suffix}_wind10_{now.strftime('%Y%m%d_%H%M%S')}.grib2"

    for ds in datasets:
        try:
            ds.close()
        except Exception:
            pass

    return {
        "filename": filename,
        "source": source_label,
        "variable": "wind10",
        "uploaded_at": now,
        "lat_min": round(float(lats_s.min()), 4),
        "lat_max": round(float(lats_s.max()), 4),
        "lon_min": round(float(lons_s.min()), 4),
        "lon_max": round(float(lons_s.max()), 4),
        "time_start": "T+024h",
        "time_end": "T+240h",
        "time_steps": 1,
        "precip_min": round(float(flat.min()), 2),
        "precip_max": round(float(flat.max()), 2),
        "precip_mean": round(float(flat.mean()), 2),
        "geojson": geojson,
        "lead_time_stats": None,
    }


# ── Public API ────────────────────────────────────────────────────────────────

async def fetch_ecmwf_forecast(
    lat_min: float = 0.0,
    lat_max: float = 35.0,
    lon_min: float = 60.0,
    lon_max: float = 155.0,
    run_time: int = 0,
    use_ensemble: bool = False,
    target_date: Optional[str] = None,
    variable: str = "tp",
) -> Optional[dict]:
    """
    Download and process the latest ECMWF IFS forecast.

    Returns a dict with all fields needed for ForecastUpload, or None on failure.

    Args:
        lat_min/max, lon_min/max: Bounding box for spatial subset.
        run_time: IFS run hour (0, 6, 12, 18 UTC). Default 0 = 00z run.
        use_ensemble: Fetch 51-member ENS (pf) instead of HRES deterministic.
        target_date: "YYYYMMDD" string, or None for latest available run.
        variable: One of "tp", "2t", "wind10", "msl". Default "tp".
    """
    try:
        from ecmwf.opendata import Client
    except ImportError:
        logger.error(
            "ecmwf-opendata not installed. "
            "Install with: pip install ecmwf-opendata cfgrib"
        )
        return None

    try:
        import cfgrib  # noqa: F401
    except ImportError:
        logger.error("cfgrib not installed. Install with: pip install cfgrib")
        return None

    if variable not in _PARAM_MAP:
        logger.error("ECMWF: unknown variable '%s'. Supported: %s", variable, list(_PARAM_MAP))
        return None

    is_ensemble = use_ensemble
    forecast_type = "pf" if is_ensemble else "fc"
    steps = _ENS_STEPS if is_ensemble else _HRES_STEPS
    source_label = "ECMWF-IFS-ENS" if is_ensemble else "ECMWF-IFS-HRES"
    param = _PARAM_MAP[variable]

    with tempfile.TemporaryDirectory() as tmpdir:
        target = os.path.join(tmpdir, "ecmwf_forecast.grib2")
        client = Client(source="ecmwf")

        kwargs: dict = dict(step=steps, type=forecast_type, param=param, target=target)
        if target_date:
            kwargs["date"] = target_date
        if run_time in (0, 6, 12, 18):
            kwargs["time"] = run_time

        logger.info(
            "ECMWF Open Data: requesting %s var=%s param=%s steps=%s date=%s time=%sz",
            forecast_type.upper(), variable, param, steps, target_date or "latest", run_time,
        )
        try:
            client.retrieve(**kwargs)
        except Exception as exc:
            logger.error("ECMWF Open Data retrieve failed: %s", exc)
            return None

        if not os.path.exists(target) or os.path.getsize(target) == 0:
            logger.error("ECMWF: downloaded file missing or empty at %s", target)
            return None

        file_size_kb = os.path.getsize(target) / 1024
        logger.info("ECMWF: downloaded %.1f KB, processing var=%s...", file_size_kb, variable)

        return _process_grib(
            target, source_label,
            lat_min, lat_max, lon_min, lon_max,
            is_ensemble, variable,
        )
