"""CSV ingestion for station/AWS observations."""
from __future__ import annotations

import csv
import io
import logging
from datetime import date

logger = logging.getLogger(__name__)

# Map of recognised column names → internal field names
_COL_MAP = {
    "date": "obs_date", "obs_date": "obs_date",
    "station_id": "station_id", "station": "station_id",
    "precip": "precip_mm", "precip_mm": "precip_mm",
    "rainfall": "precip_mm", "rain_mm": "precip_mm",
    "tmax": "temp_max_c", "temp_max": "temp_max_c", "temp_max_c": "temp_max_c",
    "tmin": "temp_min_c", "temp_min": "temp_min_c", "temp_min_c": "temp_min_c",
    "tmean": "temp_mean_c", "temp_mean": "temp_mean_c", "temp_mean_c": "temp_mean_c",
    "temp": "temp_mean_c",
    "humidity": "humidity_pct", "rh": "humidity_pct", "humidity_pct": "humidity_pct",
    "wind": "wind_speed_ms", "wind_speed": "wind_speed_ms", "wind_speed_ms": "wind_speed_ms",
    "pressure": "pressure_hpa", "pressure_hpa": "pressure_hpa", "slp": "pressure_hpa",
}

_FLOAT_FIELDS = {
    "precip_mm", "temp_max_c", "temp_min_c", "temp_mean_c",
    "humidity_pct", "wind_speed_ms", "pressure_hpa",
}

_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y%m%d"]


def _parse_date(raw: str) -> date | None:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return date.fromisoformat(raw) if fmt == "%Y-%m-%d" else __import__("datetime").datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(raw: str) -> float | None:
    raw = raw.strip()
    if raw in ("", "NA", "N/A", "null", "NULL", "-", "nan", "NaN"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_csv(
    content: bytes,
    default_station_id: str | None = None,
) -> tuple[list[dict], list[str]]:
    """Parse CSV bytes into a list of observation dicts and a list of error strings.

    Accepted CSV columns (case-insensitive, flexible names):
      station_id, date, precip_mm, temp_max_c, temp_min_c, temp_mean_c,
      humidity_pct, wind_speed_ms, pressure_hpa

    If `default_station_id` is set and the CSV has no station_id column,
    all rows are assigned to that station.

    Returns (rows, errors) where each row has at minimum: station_id, obs_date.
    """
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["CSV has no header row or is empty"]

    # Map headers to internal names (case-insensitive)
    header_map: dict[str, str] = {}
    for col in reader.fieldnames:
        key = col.strip().lower()
        if key in _COL_MAP:
            header_map[col] = _COL_MAP[key]

    rows: list[dict] = []
    errors: list[str] = []

    for i, row in enumerate(reader, start=2):  # start=2 because row 1 is header
        mapped: dict[str, str] = {header_map[k]: v for k, v in row.items() if k in header_map}

        # Station ID
        sid = mapped.get("station_id", "").strip() or default_station_id
        if not sid:
            errors.append(f"Row {i}: missing station_id")
            continue

        # Date
        raw_date = mapped.get("obs_date", "")
        obs_date = _parse_date(raw_date)
        if obs_date is None:
            errors.append(f"Row {i}: unparseable date '{raw_date}'")
            continue

        rec: dict = {"station_id": sid, "obs_date": obs_date}
        for field in _FLOAT_FIELDS:
            if field in mapped:
                val = _parse_float(mapped[field])
                if val is not None:
                    rec[field] = val

        rows.append(rec)

    return rows, errors
