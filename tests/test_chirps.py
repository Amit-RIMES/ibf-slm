"""Unit tests for app/core/chirps.py — no HTTP, no DB."""
import gzip
import io
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import tifffile

from app.core.chirps import (
    _CHIRPS_COLS,
    _CHIRPS_NODATA,
    _CHIRPS_ROWS,
    _build_geojson,
    _row_col_bounds,
    fetch_chirps_day,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_tif_gz(value: float = 10.0) -> bytes:
    """Return a minimal valid CHIRPS-shaped GeoTIFF.gz."""
    arr = np.full((_CHIRPS_ROWS, _CHIRPS_COLS), _CHIRPS_NODATA, dtype=np.float32)
    # Rain over SE Asia (lat 0-35, lon 60-155) → rows 300-1000, cols 4800-6700
    arr[300:1000, 4800:6700] = value
    buf = io.BytesIO()
    tifffile.imwrite(buf, arr)
    return gzip.compress(buf.getvalue())


def _mock_http(status: int, content: bytes):
    resp = MagicMock()
    resp.status_code = status
    resp.content = content

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=resp)
    return mock_client


# ── _row_col_bounds ───────────────────────────────────────────────────────────

async def test_row_col_bounds_se_asia():
    row_min, row_max, col_min, col_max = _row_col_bounds(0.0, 35.0, 60.0, 155.0)
    # lat 35N → row (50-35)/0.05 = 300
    assert row_min == 300
    # lat 0N  → row (50-0)/0.05  = 1000
    assert row_max == 1000
    # lon 60E → col (60+180)/0.05 = 4800
    assert col_min == 4800
    # lon 155E→ col (155+180)/0.05 = 6700
    assert col_max == 6700


async def test_row_col_bounds_clamps_to_grid():
    # Out-of-range values should clamp to valid grid indices
    row_min, row_max, col_min, col_max = _row_col_bounds(-60.0, 60.0, -200.0, 200.0)
    assert row_min >= 0
    assert row_max <= _CHIRPS_ROWS - 1
    assert col_min >= 0
    assert col_max <= _CHIRPS_COLS - 1


# ── _build_geojson ────────────────────────────────────────────────────────────

async def test_build_geojson_returns_feature_collection():
    subset = np.full((100, 100), 20.0, dtype=np.float32)
    result = _build_geojson(subset, row_min=300, col_min=4800, step=10)
    import json
    gj = json.loads(result)
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) > 0


async def test_build_geojson_skips_nodata():
    subset = np.full((100, 100), _CHIRPS_NODATA, dtype=np.float32)
    import json
    gj = json.loads(_build_geojson(subset, 300, 4800, 10))
    assert gj["features"] == []


async def test_build_geojson_intensity_capped_at_one():
    subset = np.full((50, 50), 200.0, dtype=np.float32)  # 200mm >> 80mm cap
    import json
    gj = json.loads(_build_geojson(subset, 300, 4800, 10))
    for f in gj["features"]:
        assert f["properties"]["intensity"] <= 1.0


# ── fetch_chirps_day ──────────────────────────────────────────────────────────

async def test_fetch_chirps_day_not_available():
    mock_client = _mock_http(404, b"")
    with patch("app.core.chirps.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_chirps_day(date(2026, 1, 1))
    assert result is None


async def test_fetch_chirps_day_success_returns_stats():
    gz = _make_tif_gz(value=25.0)
    mock_client = _mock_http(200, gz)
    with patch("app.core.chirps.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_chirps_day(
            date(2026, 1, 1),
            lat_min=0.0, lat_max=35.0, lon_min=60.0, lon_max=155.0,
        )
    assert result is not None
    assert result["precip_mean"] == pytest.approx(25.0, abs=0.1)
    assert result["precip_max"] == pytest.approx(25.0, abs=0.1)
    assert result["wet_fraction"] == pytest.approx(1.0, abs=0.01)
    assert result["source"] == "CHIRPS"
    assert result["obs_date"] == date(2026, 1, 1)
    assert result["pixel_count"] > 0
    assert result["geojson"] is not None


async def test_fetch_chirps_day_prelim_tried_first():
    """First successful URL marks result as preliminary."""
    gz = _make_tif_gz(value=5.0)
    call_urls = []

    async def mock_get(url, **kwargs):
        call_urls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.content = gz
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = mock_get

    with patch("app.core.chirps.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_chirps_day(date(2026, 1, 1))

    assert result is not None
    assert result["is_preliminary"] is True
    # Should have tried prelim URL first
    assert "prelim" in call_urls[0]


async def test_fetch_chirps_day_falls_back_to_final():
    """When prelim returns 404, tries final."""
    gz = _make_tif_gz(value=5.0)
    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        if "prelim" in url:
            resp.status_code = 404
            resp.content = b""
        else:
            resp.status_code = 200
            resp.content = gz
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = mock_get

    with patch("app.core.chirps.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_chirps_day(date(2026, 1, 1))

    assert result is not None
    assert result["is_preliminary"] is False
    assert call_count == 2  # tried both URLs
