"""
CHIRPS v2.0 daily observed rainfall ingestion.

Downloads CHIRPS p05 (0.05°) GeoTIFF.gz files from UCSB CHC, subsets to a
configurable region, computes spatial statistics, and returns an ObservedRainfall
record ready to persist.

CHIRPS p05 global grid:
  - Extent  : 180°W–180°E, 50°S–50°N
  - Resolution: 0.05° (~5.5 km)
  - Dimensions: 7200 cols × 2000 rows
  - Nodata  : -9999
  - Origin  : top-left corner = (-180.0, 50.0)

URLs:
  Final     : https://data.chc.ucsb.edu/products/CHIRPS-2.0/global_daily/tifs/p05/{year}/chirps-v2.0.{Y}.{M}.{D}.tif.gz
  Preliminary: https://data.chc.ucsb.edu/products/CHIRPS-2.0/prelim/global_daily/tifs/p05/{year}/chirps-v2.0.{Y}.{M}.{D}.tif.gz
"""
import gzip
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
import numpy as np
import tifffile

logger = logging.getLogger(__name__)

# CHIRPS p05 global grid constants
_CHIRPS_LON_ORIGIN = -180.0
_CHIRPS_LAT_ORIGIN = 50.0   # top-left (north)
_CHIRPS_RES = 0.05
_CHIRPS_COLS = 7200
_CHIRPS_ROWS = 2000
_CHIRPS_NODATA = -9999.0

_BASE_FINAL = (
    "https://data.chc.ucsb.edu/products/CHIRPS-2.0"
    "/global_daily/tifs/p05/{year}/chirps-v2.0.{year}.{month:02d}.{day:02d}.tif.gz"
)
_BASE_PRELIM = (
    "https://data.chc.ucsb.edu/products/CHIRPS-2.0"
    "/prelim/global_daily/tifs/p05/{year}/chirps-v2.0.{year}.{month:02d}.{day:02d}.tif.gz"
)

# GeoJSON downsample step (10 × 0.05° = 0.5° cells)
_GEOJSON_STEP = 10
_MAX_GEOJSON_CELLS = 4000  # safety cap


def _row_col_bounds(lat_min: float, lat_max: float, lon_min: float, lon_max: float):
    """Convert lat/lon bounds to CHIRPS pixel row/col indices (inclusive)."""
    # row increases southward from lat 50°
    row_min = max(0, int(((_CHIRPS_LAT_ORIGIN - lat_max) / _CHIRPS_RES)))
    row_max = min(_CHIRPS_ROWS - 1, int(((_CHIRPS_LAT_ORIGIN - lat_min) / _CHIRPS_RES)))
    col_min = max(0, int(((lon_min - _CHIRPS_LON_ORIGIN) / _CHIRPS_RES)))
    col_max = min(_CHIRPS_COLS - 1, int(((lon_max - _CHIRPS_LON_ORIGIN) / _CHIRPS_RES)))
    return row_min, row_max, col_min, col_max


def _build_geojson(
    subset: np.ndarray, row_min: int, col_min: int, step: int
) -> str:
    """Build a GeoJSON FeatureCollection from a downsampled precipitation grid."""
    features = []
    sampled = subset[::step, ::step]
    half = step * _CHIRPS_RES / 2.0

    for r in range(sampled.shape[0]):
        for c in range(sampled.shape[1]):
            val = float(sampled[r, c])
            if val < 0:
                continue
            # Centre of downsampled cell
            lat_c = _CHIRPS_LAT_ORIGIN - (row_min + r * step) * _CHIRPS_RES - half
            lon_c = _CHIRPS_LON_ORIGIN + (col_min + c * step) * _CHIRPS_RES + half
            intensity = round(min(val / 80.0, 1.0), 3)  # 80 mm/day = full colour
            features.append({
                "type": "Feature",
                "properties": {"precip": round(val, 2), "intensity": intensity},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [lon_c - half, lat_c - half],
                        [lon_c + half, lat_c - half],
                        [lon_c + half, lat_c + half],
                        [lon_c - half, lat_c + half],
                        [lon_c - half, lat_c - half],
                    ]],
                },
            })
            if len(features) >= _MAX_GEOJSON_CELLS:
                break
        if len(features) >= _MAX_GEOJSON_CELLS:
            break

    return json.dumps({"type": "FeatureCollection", "features": features})


async def fetch_chirps_day(
    obs_date: date,
    lat_min: float = 0.0,
    lat_max: float = 35.0,
    lon_min: float = 60.0,
    lon_max: float = 155.0,
    timeout: float = 120.0,
) -> Optional[dict]:
    """
    Download and parse one day of CHIRPS data. Returns a dict with all fields
    needed to create an ObservedRainfall record, or None if the data is not
    yet available.

    Tries the preliminary product first (1-2 day lag), then the final
    product (3-5 day lag). Returns None if neither is available.
    """
    y, m, d = obs_date.year, obs_date.month, obs_date.day
    urls = [
        (_BASE_PRELIM.format(year=y, month=m, day=d), True),
        (_BASE_FINAL.format(year=y, month=m, day=d), False),
    ]

    raw: Optional[bytes] = None
    is_preliminary = False

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url, prelim in urls:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    raw = resp.content
                    is_preliminary = prelim
                    logger.info(
                        "CHIRPS %s fetched %s (%.1f KB, %s)",
                        obs_date, url.split("/")[-1], len(raw) / 1024,
                        "preliminary" if prelim else "final",
                    )
                    break
                # 404 means not available yet, try next
            except Exception as exc:
                logger.warning("CHIRPS fetch error for %s from %s: %s", obs_date, url, exc)

    if raw is None:
        logger.info("CHIRPS data not yet available for %s", obs_date)
        return None

    # Decompress and parse
    try:
        tif_bytes = gzip.decompress(raw)
        arr = tifffile.imread(io.BytesIO(tif_bytes)).astype(np.float32)
    except Exception as exc:
        logger.error("CHIRPS parse error for %s: %s", obs_date, exc)
        return None

    # Validate shape
    if arr.ndim != 2 or arr.shape != (_CHIRPS_ROWS, _CHIRPS_COLS):
        logger.error(
            "CHIRPS unexpected shape %s for %s (expected %dx%d)",
            arr.shape, obs_date, _CHIRPS_ROWS, _CHIRPS_COLS,
        )
        return None

    # Subset to region of interest
    row_min, row_max, col_min, col_max = _row_col_bounds(lat_min, lat_max, lon_min, lon_max)
    subset = arr[row_min:row_max + 1, col_min:col_max + 1]

    valid = subset[subset != _CHIRPS_NODATA]
    if valid.size == 0:
        logger.warning("CHIRPS: no valid pixels for %s in region", obs_date)
        return None

    wet = valid[valid >= 1.0]

    geojson = _build_geojson(subset, row_min, col_min, _GEOJSON_STEP)

    return {
        "obs_date": obs_date,
        "source": "CHIRPS",
        "lat_min": lat_min,
        "lat_max": lat_max,
        "lon_min": lon_min,
        "lon_max": lon_max,
        "precip_mean": round(float(np.mean(valid)), 4),
        "precip_max": round(float(np.max(valid)), 4),
        "precip_min": round(float(np.min(valid[valid >= 0])) if np.any(valid >= 0) else 0.0, 4),
        "wet_fraction": round(float(len(wet) / len(valid)), 4),
        "pixel_count": int(len(valid)),
        "is_preliminary": is_preliminary,
        "geojson": geojson,
        "fetched_at": datetime.now(timezone.utc),
    }


async def sync_recent_days(
    db,
    lookback_days: int = 7,
    lat_min: float = 0.0,
    lat_max: float = 35.0,
    lon_min: float = 60.0,
    lon_max: float = 155.0,
) -> list[date]:
    """
    Fetch up to `lookback_days` of CHIRPS data ending yesterday.
    Skips dates already present in the DB.
    Returns list of newly ingested dates.
    """
    from sqlalchemy import select
    from app.models.observed_rainfall import ObservedRainfall

    today = datetime.now(timezone.utc).date()
    ingested = []

    for delta in range(1, lookback_days + 1):
        obs_date = today - timedelta(days=delta)

        # Skip if already ingested
        existing = await db.execute(
            select(ObservedRainfall.id).where(
                ObservedRainfall.obs_date == obs_date,
                ObservedRainfall.source == "CHIRPS",
            )
        )
        if existing.scalar_one_or_none() is not None:
            continue

        data = await fetch_chirps_day(obs_date, lat_min, lat_max, lon_min, lon_max)
        if data is None:
            continue

        record = ObservedRainfall(**data)
        db.add(record)
        await db.commit()
        await db.refresh(record)
        ingested.append(obs_date)
        logger.info("CHIRPS ingested %s (mean=%.2f mm)", obs_date, data["precip_mean"])

    return ingested


async def backfill_range(
    db,
    start_date: date,
    end_date: date,
    lat_min: float = 0.0,
    lat_max: float = 35.0,
    lon_min: float = 60.0,
    lon_max: float = 155.0,
    delay_seconds: float = 0.5,
) -> tuple[int, int, int]:
    """
    Download CHIRPS data for every date in [start_date, end_date].
    Skips dates already in the DB and never fetches today or future dates.
    A short delay between requests avoids hammering the UCSB server.

    Returns (ingested, skipped, errors).
    """
    import asyncio
    from sqlalchemy import select
    from app.models.observed_rainfall import ObservedRainfall

    today = datetime.now(timezone.utc).date()
    end_date = min(end_date, today - timedelta(days=1))
    if start_date > end_date:
        return 0, 0, 0

    # Pre-fetch the set of existing dates to skip DB hits per day
    existing_result = await db.execute(
        select(ObservedRainfall.obs_date).where(
            ObservedRainfall.obs_date >= start_date,
            ObservedRainfall.obs_date <= end_date,
            ObservedRainfall.source == "CHIRPS",
        )
    )
    existing_dates: set[date] = {r[0] for r in existing_result.all()}

    total_days = (end_date - start_date).days + 1
    ingested = skipped = errors = 0
    current = start_date

    while current <= end_date:
        if current in existing_dates:
            skipped += 1
            current += timedelta(days=1)
            continue

        try:
            data = await fetch_chirps_day(current, lat_min, lat_max, lon_min, lon_max)
            if data is None:
                errors += 1
            else:
                db.add(ObservedRainfall(**data))
                await db.commit()
                ingested += 1
                done = ingested + errors
                logger.info(
                    "CHIRPS backfill: %s (%.2f mm) [%d/%d processed, %d skipped]",
                    current, data["precip_mean"], done, total_days - skipped, skipped,
                )
        except Exception as exc:
            logger.error("CHIRPS backfill error for %s: %s", current, exc)
            errors += 1

        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        current += timedelta(days=1)

    logger.info(
        "CHIRPS backfill complete: %d ingested, %d skipped, %d errors",
        ingested, skipped, errors,
    )
    return ingested, skipped, errors
