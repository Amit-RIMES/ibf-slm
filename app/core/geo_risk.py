"""Country-level risk rollup for the public status page.

Nothing in the schema ties an alert directly to a country. This module derives
a per-country, per-hazard risk level from data that already exists:
  - Trigger geographic scope (polygon/bbox) + ForecastUpload.source country keys
  - ImpactRecord.country (free text) as a secondary, recent-history signal

Never fabricates a risk level for a hazard nobody monitors in a country —
absence of data means the hazard card is omitted, not shown as "low".
"""
from __future__ import annotations

import time
from typing import Literal, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.trigger import Trigger, TriggerActivation
from app.routers.map_view import _load_countries, _point_in_feature

RiskLevel = Literal["low", "moderate", "high", "extreme"]

# ── Naming-convention bridges between COUNTRY_NAMES values and countries.geojson `name` ──
_COUNTRY_NAME_ALIASES: dict[str, str] = {
    "Bahamas": "The Bahamas",
    "Central African Rep.": "Central African Republic",
    "Congo": "Republic of the Congo",
    "Czechia": "Czech Republic",
    "Côte d'Ivoire": "Ivory Coast",
    "DR Congo": "Democratic Republic of the Congo",
    "Eswatini": "Swaziland",
    "Guinea-Bissau": "Guinea Bissau",
    "North Macedonia": "Macedonia",
    "Palestine": "West Bank",
    "Russian Federation": "Russia",
    "Serbia": "Republic of Serbia",
    "Tanzania": "United Republic of Tanzania",
    "Timor-Leste": "East Timor",
    "United Kingdom": "England",
    "United States": "USA",
}

# Small island/micro-states absent from the 110m countries.geojson entirely —
# no polygon exists, so risk falls back to source-match only, and the map
# preview falls back to this capital-city centroid marker.
_MICROSTATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "Andorra": (42.50, 1.52), "Antigua and Barbuda": (17.12, -61.85),
    "Bahrain": (26.23, 50.58), "Barbados": (13.10, -59.62),
    "Cape Verde": (14.93, -23.51), "Comoros": (-11.70, 43.26),
    "Dominica": (15.30, -61.39), "Grenada": (12.06, -61.75),
    "Holy See (Vatican City State)": (41.90, 12.45), "Kiribati": (1.45, 173.02),
    "Liechtenstein": (47.14, 9.52), "Maldives": (4.17, 73.51),
    "Malta": (35.90, 14.51), "Marshall Islands": (7.12, 171.38),
    "Mauritius": (-20.16, 57.50), "Micronesia": (6.92, 158.16),
    "Monaco": (43.74, 7.42), "Nauru": (-0.55, 166.92),
    "Palau": (7.50, 134.62), "Saint Kitts and Nevis": (17.30, -62.72),
    "Saint Lucia": (14.01, -60.99), "Saint Vincent and the Grenadines": (13.16, -61.22),
    "Samoa": (-13.83, -171.76), "San Marino": (43.94, 12.46),
    "Seychelles": (-4.62, 55.45), "Singapore": (1.35, 103.82),
    "São Tomé & Príncipe": (0.19, 6.61), "Tonga": (-21.14, -175.20),
    "Tuvalu": (-8.52, 179.20),
}

_IMPACT_WINDOW_DAYS = 30
_CACHE_TTL_SECONDS = 90

_geo_name_index: dict[str, dict] | None = None
_raw_cache: tuple[float, list, list] | None = None  # (fetched_at, activations, monitored_triggers)


def _geojson_name_index() -> dict[str, dict]:
    """name -> geometry, built once from the cached countries.geojson."""
    global _geo_name_index
    if _geo_name_index is None:
        countries = _load_countries()
        _geo_name_index = {
            feat["properties"]["name"]: feat["geometry"]
            for feat in countries.get("features", [])
            if feat.get("geometry")
        }
    return _geo_name_index


def _resolve_geojson_name(country_name: str) -> Optional[str]:
    index = _geojson_name_index()
    if country_name in index:
        return country_name
    alias = _COUNTRY_NAME_ALIASES.get(country_name)
    if alias and alias in index:
        return alias
    return None


def _trigger_centroid(trig: Trigger, fc: Optional[ForecastUpload]) -> Optional[tuple[float, float]]:
    """Same 3-step fallback as alerts.py's _build_heatmap: polygon -> bbox -> forecast bbox."""
    import json as _json

    if trig.scope_polygon:
        try:
            ring = _json.loads(trig.scope_polygon)
            lons_r = [p[0] for p in ring]
            lats_r = [p[1] for p in ring]
            return sum(lats_r) / len(lats_r), sum(lons_r) / len(lons_r)
        except Exception:
            pass
    if trig.scope_lat_min is not None and trig.scope_lat_max is not None:
        return (
            (trig.scope_lat_min + trig.scope_lat_max) / 2,
            (trig.scope_lon_min + trig.scope_lon_max) / 2,
        )
    if fc is not None and fc.lat_min is not None:
        return (fc.lat_min + fc.lat_max) / 2, (fc.lon_min + fc.lon_max) / 2
    return None


def _trigger_bbox(trig: Trigger, fc: Optional[ForecastUpload]) -> Optional[tuple[float, float, float, float]]:
    """(lat_min, lat_max, lon_min, lon_max), same source priority as _trigger_centroid."""
    import json as _json

    if trig.scope_polygon:
        try:
            ring = _json.loads(trig.scope_polygon)
            lons_r = [p[0] for p in ring]
            lats_r = [p[1] for p in ring]
            return min(lats_r), max(lats_r), min(lons_r), max(lons_r)
        except Exception:
            pass
    if trig.scope_lat_min is not None and trig.scope_lat_max is not None:
        return trig.scope_lat_min, trig.scope_lat_max, trig.scope_lon_min, trig.scope_lon_max
    if fc is not None and fc.lat_min is not None:
        return fc.lat_min, fc.lat_max, fc.lon_min, fc.lon_max
    return None


_SAMPLE_GRID_N = 7  # 7x7 = 49 points — cheap (only used for MultiPolygon/archipelago
# countries), but needs real density: individual islands at 110m simplification can be
# narrow enough that a sparser grid (e.g. 3x3) lands entirely between them by bad luck.


def _sample_points(bbox: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    """N x N grid spanning a bbox, used only as the archipelago fallback."""
    lat_min, lat_max, lon_min, lon_max = bbox
    n = _SAMPLE_GRID_N
    if n == 1:
        return [((lat_min + lat_max) / 2, (lon_min + lon_max) / 2)]
    lats = [lat_min + (lat_max - lat_min) * i / (n - 1) for i in range(n)]
    lons = [lon_min + (lon_max - lon_min) * j / (n - 1) for j in range(n)]
    return [(lat, lon) for lat in lats for lon in lons]


def _resolves_to_country(
    trig: Trigger, fc: Optional[ForecastUpload], country_iso2: str, geom: Optional[dict],
) -> bool:
    """(a) direct ForecastUpload.source == country_{iso2} match; else
    (b) the trigger's centroid falls inside the country polygon — the strict,
    precise test, used for every mainland/contiguous country; else
    (c) ONLY for MultiPolygon countries (archipelagos — Philippines, Indonesia,
    Japan, ...), also try a dense sample grid across the trigger's bbox. A bbox
    centroid can land in open sea between islands and miss a single-point test,
    but widening this to mainland countries too would false-match merely
    adjacent countries (e.g. a Bangladesh-scoped trigger's bbox corner can sit
    inside India's coarse 110m polygon, since Bangladesh is embedded within it)."""
    if fc is not None and fc.source == f"country_{country_iso2}":
        return True
    if geom is None:
        return False
    centroid = _trigger_centroid(trig, fc)
    if centroid is not None and _point_in_feature(centroid[0], centroid[1], geom):
        return True
    if geom.get("type") != "MultiPolygon":
        return False
    bbox = _trigger_bbox(trig, fc)
    if bbox is None:
        return False
    return any(_point_in_feature(lat, lon, geom) for lat, lon in _sample_points(bbox))


async def _fetch_raw(db: AsyncSession) -> tuple[list, list]:
    """All active (activation, trigger, forecast) rows + all monitored triggers.
    Shared across every visitor regardless of country, so cached briefly to
    decouple DB load from public traffic volume."""
    global _raw_cache
    now = time.monotonic()
    if _raw_cache is not None and (now - _raw_cache[0]) < _CACHE_TTL_SECONDS:
        return _raw_cache[1], _raw_cache[2]

    active_result = await db.execute(
        select(TriggerActivation)
        .join(Trigger, TriggerActivation.trigger_id == Trigger.id)
        .where(TriggerActivation.status == "active")
        .where(Trigger.is_active == True)  # noqa: E712
    )
    activations = active_result.scalars().all()

    monitored_result = await db.execute(select(Trigger).where(Trigger.is_active == True))  # noqa: E712
    monitored = monitored_result.scalars().all()

    _raw_cache = (now, activations, monitored)
    return activations, monitored


async def compute_country_risk(db: AsyncSession, country_iso2: str, country_name: str) -> dict[str, dict]:
    """{hazard_type: {"level": RiskLevel, "trigger_count": int}}.
    Hazards with no signal for this country are omitted entirely."""
    activations, monitored = await _fetch_raw(db)
    geojson_name = _resolve_geojson_name(country_name)
    geom = _geojson_name_index().get(geojson_name) if geojson_name else None

    by_hazard_active: dict[str, set[int]] = {}
    for act in activations:
        trig, fc = act.trigger, act.forecast
        if _resolves_to_country(trig, fc, country_iso2, geom):
            by_hazard_active.setdefault(trig.hazard_type, set()).add(trig.id)

    by_hazard_monitored: dict[str, bool] = {}
    for trig in monitored:
        if trig.hazard_type in by_hazard_monitored:
            continue
        if _resolves_to_country(trig, None, country_iso2, geom):
            by_hazard_monitored[trig.hazard_type] = True

    recent_impact_hazards = await _recent_impact_hazards(db, country_name)

    all_hazards = set(by_hazard_active) | set(by_hazard_monitored) | recent_impact_hazards
    result: dict[str, dict] = {}
    for hazard in all_hazards:
        trigger_ids = by_hazard_active.get(hazard, set())
        if trigger_ids:
            level: RiskLevel = "extreme" if len(trigger_ids) >= 2 else "high"
            result[hazard] = {"level": level, "trigger_count": len(trigger_ids)}
            continue
        if hazard in recent_impact_hazards:
            result[hazard] = {"level": "moderate", "trigger_count": 0}
            continue
        if by_hazard_monitored.get(hazard):
            result[hazard] = {"level": "low", "trigger_count": 0}
        # else: nobody monitors this hazard here at all — omit.

    return result


async def _recent_impact_hazards(db: AsyncSession, country_name: str) -> set[str]:
    """Hazard types with an ImpactRecord for this country within the recent window."""
    from datetime import date, timedelta

    cutoff = date.today() - timedelta(days=_IMPACT_WINDOW_DAYS)
    names = {country_name}
    alias = _COUNTRY_NAME_ALIASES.get(country_name)
    if alias:
        names.add(alias)
    from sqlalchemy import or_

    conditions = or_(*(ImpactRecord.country.ilike(n) for n in names))
    rows = await db.execute(
        select(ImpactRecord.hazard_type)
        .where(conditions)
        .where(ImpactRecord.event_date >= cutoff)
        .distinct()
    )
    return {r[0] for r in rows.all()}


async def get_regional_advisories(db: AsyncSession) -> list[dict]:
    """Active triggers whose geographic scope can't be resolved to any single
    country (no polygon/bbox, no country-source forecast). Never attributed
    to a specific country — shown as a general 'regional watch' note."""
    activations, _ = await _fetch_raw(db)
    seen: dict[int, dict] = {}
    for act in activations:
        trig, fc = act.trigger, act.forecast
        has_source_country = fc is not None and fc.source and fc.source.startswith("country_")
        has_geo_scope = _trigger_centroid(trig, fc) is not None
        if has_source_country or has_geo_scope:
            continue
        seen[trig.id] = {"hazard_type": trig.hazard_type, "trigger_name": trig.name}
    return list(seen.values())


def microstate_centroid(country_name: str) -> Optional[tuple[float, float]]:
    return _MICROSTATE_CENTROIDS.get(country_name)


def has_boundary_geometry(country_name: str) -> bool:
    return _resolve_geojson_name(country_name) is not None
