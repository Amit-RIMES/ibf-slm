"""
ECMWF Open Data (IFS) forecast ingestion.

Downloads ECMWF IFS forecasts using the free ecmwf-opendata Python client.
No API key or registration required.

Parameter:
  tp = total precipitation accumulation (metres, converted to mm here)

HRES (deterministic): 0.25° resolution, 10-day horizon
ENS  (ensemble, 51 members): 0.5° resolution, 15-day horizon

`tp` is cumulative from T+0, so lead-time buckets are derived by
differencing adjacent steps:
  d1_5  = tp(step=120h)
  d6_10 = tp(step=240h) - tp(step=120h)

System dependency: cfgrib requires the eccodes C library.
  Ubuntu/Debian: sudo apt-get install libeccodes-dev
  macOS:         brew install eccodes
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

# Steps to request (hours from forecast start)
_HRES_STEPS = [24, 48, 72, 96, 120, 144, 168, 192, 216, 240]
_ENS_STEPS = [24, 120, 240]


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


def _process_grib(
    path: str,
    source_label: str,
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    is_ensemble: bool,
) -> Optional[dict]:
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

    tp_name = next((n for n in ("tp", "precipitation", "rain") if n in ds.data_vars), None)
    if tp_name is None:
        tp_name = list(ds.data_vars)[0]

    da = ds[tp_name] * _MM_PER_M  # metres → mm

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

            # Lead-time bucket: d1_5 = step at ~120h, d6_10 = step ~240h − step ~120h
            idx_5d = min(range(len(step_hours)), key=lambda i: abs(step_hours[i] - 120))
            idx_10d = min(range(len(step_hours)), key=lambda i: abs(step_hours[i] - 240))
            lt = {}
            arr_5d = da.isel({step_dim: idx_5d}).values
            lt["d1_5"] = _bucket_stats(arr_5d)
            if idx_10d != idx_5d:
                arr_10d = da.isel({step_dim: idx_10d}).values
                arr_6_10 = np.where(arr_10d - arr_5d >= 0, arr_10d - arr_5d, 0)
                lt["d6_10"] = _bucket_stats(arr_6_10)
            lead_time_stats = json.dumps(lt)
        except Exception as exc:
            logger.warning("ECMWF lead-time stats failed: %s", exc)

        # Use the final step (longest accumulation) as the total precip field
        da_total = da.isel({step_dim: -1})
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
    filename = f"ecmwf_ifs_{suffix}_{now.strftime('%Y%m%d_%H%M%S')}.grib2"

    ds.close()

    return {
        "filename": filename,
        "source": source_label,
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


# ── Public API ────────────────────────────────────────────────────────────────

async def fetch_ecmwf_forecast(
    lat_min: float = 0.0,
    lat_max: float = 35.0,
    lon_min: float = 60.0,
    lon_max: float = 155.0,
    run_time: int = 0,
    use_ensemble: bool = False,
    target_date: Optional[str] = None,
) -> Optional[dict]:
    """
    Download and process the latest ECMWF IFS forecast.

    Returns a dict with all fields needed for ForecastUpload, or None on failure.

    Args:
        lat_min/max, lon_min/max: Bounding box for spatial subset.
        run_time: IFS run hour (0, 6, 12, 18 UTC). Default 0 = 00z run.
        use_ensemble: Fetch 51-member ENS (pf) instead of HRES deterministic.
        target_date: "YYYYMMDD" string, or None for latest available run.
    """
    try:
        from ecmwf.opendata import Client
    except ImportError:
        logger.error(
            "ecmwf-opendata not installed. "
            "Install with: pip install ecmwf-opendata cfgrib "
            "and ensure eccodes is available (apt install libeccodes-dev)."
        )
        return None

    try:
        import cfgrib  # noqa: F401  — just verify it's importable
    except ImportError:
        logger.error(
            "cfgrib not installed. Install with: pip install cfgrib "
            "and ensure eccodes is available (apt install libeccodes-dev)."
        )
        return None

    is_ensemble = use_ensemble
    forecast_type = "pf" if is_ensemble else "fc"
    steps = _ENS_STEPS if is_ensemble else _HRES_STEPS
    source_label = "ECMWF-IFS-ENS" if is_ensemble else "ECMWF-IFS-HRES"

    with tempfile.TemporaryDirectory() as tmpdir:
        target = os.path.join(tmpdir, "ecmwf_forecast.grib2")
        client = Client(source="ecmwf")

        kwargs: dict = dict(step=steps, type=forecast_type, param="tp", target=target)
        if target_date:
            kwargs["date"] = target_date
        if run_time in (0, 6, 12, 18):
            kwargs["time"] = run_time

        logger.info(
            "ECMWF Open Data: requesting %s tp steps=%s date=%s time=%sz",
            forecast_type.upper(), steps, target_date or "latest", run_time,
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
        logger.info("ECMWF: downloaded %.1f KB, processing...", file_size_kb)

        return _process_grib(
            target, source_label,
            lat_min, lat_max, lon_min, lon_max,
            is_ensemble,
        )
