"""Tests for Batch 18: ECMWF Open Data integration."""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


# ── Auth / access control ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ecmwf_page_requires_auth(client: AsyncClient, db: AsyncSession):
    resp = await client.get("/ecmwf", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_ecmwf_page_admin_only(client: AsyncClient, db: AsyncSession):
    from app.core.security import hash_password
    from app.models.user import User
    viewer = User(email="viewer_ecmwf@test.com", username="viewer_ecmwf",
                  hashed_password=hash_password("Viewer1234"),
                  is_active=True, role="viewer")
    db.add(viewer)
    await db.commit()

    await _login(client, email="viewer_ecmwf@test.com", password="Viewer1234")
    resp = await client.get("/ecmwf", follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_ecmwf_page_loads_for_admin(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/ecmwf", follow_redirects=False)
    assert resp.status_code == 200
    assert b"ECMWF Open Data" in resp.content


# ── Page content ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ecmwf_page_shows_config_form(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/ecmwf", follow_redirects=False)
    assert resp.status_code == 200
    content = resp.text
    assert "lat_min" in content
    assert "lat_max" in content
    assert "lon_min" in content
    assert "lon_max" in content
    assert "run_time" in content
    assert "sync_hour" in content


@pytest.mark.asyncio
async def test_ecmwf_page_shows_no_forecasts_message(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/ecmwf", follow_redirects=False)
    assert resp.status_code == 200
    assert b"No ECMWF IFS forecasts ingested yet" in resp.content


@pytest.mark.asyncio
async def test_ecmwf_page_shows_recent_forecasts(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.forecast import ForecastUpload
    fc = ForecastUpload(
        filename="ecmwf_ifs_hres_20260616_100000.grib2",
        source="ECMWF-IFS-HRES",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=0.0, lat_max=35.0, lon_min=60.0, lon_max=155.0,
        time_start="T+024h", time_end="T+240h", time_steps=10,
        precip_min=0.0, precip_max=150.5, precip_mean=42.3,
        geojson='{"type":"FeatureCollection","features":[]}',
    )
    db.add(fc)
    await db.commit()

    await _login(client)
    resp = await client.get("/ecmwf", follow_redirects=False)
    assert resp.status_code == 200
    assert b"ecmwf_ifs_hres" in resp.content
    assert b"ECMWF-IFS-HRES" in resp.content
    assert b"42.3" in resp.content


@pytest.mark.asyncio
async def test_ecmwf_page_shows_fetch_now_button(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/ecmwf", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Fetch Latest Now" in resp.content


@pytest.mark.asyncio
async def test_ecmwf_page_shows_about_section(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/ecmwf", follow_redirects=False)
    assert resp.status_code == 200
    assert b"About ECMWF Open Data" in resp.content
    assert b"HRES" in resp.content
    assert b"ENS" in resp.content


# ── Config save ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ecmwf_config_save(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    csrf = _csrf(client)
    resp = await client.post("/ecmwf/config", data={
        "csrf_token": csrf,
        "enabled": "on",
        "run_time": "12",
        "sync_hour": "14",
        "sync_minute": "30",
        "lat_min": "5.0",
        "lat_max": "28.0",
        "lon_min": "92.0",
        "lon_max": "102.0",
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    from app.models.ecmwf_config import EcmwfConfig
    cfg = await db.scalar(select(EcmwfConfig).where(EcmwfConfig.id == 1))
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.run_time == 12
    assert cfg.sync_hour == 14
    assert cfg.sync_minute == 30
    assert cfg.lat_min == pytest.approx(5.0)
    assert cfg.lon_min == pytest.approx(92.0)


@pytest.mark.asyncio
async def test_ecmwf_config_disable(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    csrf = _csrf(client)
    # enabled absent from form = off
    resp = await client.post("/ecmwf/config", data={
        "csrf_token": csrf,
        "run_time": "0",
        "sync_hour": "10",
        "sync_minute": "0",
        "lat_min": "0.0",
        "lat_max": "35.0",
        "lon_min": "60.0",
        "lon_max": "155.0",
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    from app.models.ecmwf_config import EcmwfConfig
    cfg = await db.scalar(select(EcmwfConfig).where(EcmwfConfig.id == 1))
    assert cfg is not None
    assert cfg.enabled is False


@pytest.mark.asyncio
async def test_ecmwf_config_ensemble_toggle(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    csrf = _csrf(client)
    resp = await client.post("/ecmwf/config", data={
        "csrf_token": csrf,
        "use_ensemble": "on",
        "run_time": "0",
        "sync_hour": "10",
        "sync_minute": "0",
        "lat_min": "0.0",
        "lat_max": "35.0",
        "lon_min": "60.0",
        "lon_max": "155.0",
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    from app.models.ecmwf_config import EcmwfConfig
    cfg = await db.scalar(select(EcmwfConfig).where(EcmwfConfig.id == 1))
    assert cfg.use_ensemble is True


# ── Fetch-now endpoint ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_now_requires_admin(client: AsyncClient, db: AsyncSession):
    from app.core.security import hash_password
    from app.models.user import User
    viewer = User(email="viewer_fn@test.com", username="viewer_fn",
                  hashed_password=hash_password("Viewer1234"),
                  is_active=True, role="viewer")
    db.add(viewer)
    await db.commit()

    await _login(client, email="viewer_fn@test.com", password="Viewer1234")
    csrf = _csrf(client)
    resp = await client.post("/ecmwf/fetch-now", data={"csrf_token": csrf},
                              follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_fetch_now_redirects_with_fetching_param(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    csrf = _csrf(client)
    # Patch the background task so the test doesn't actually download ECMWF data
    with patch("app.routers.ecmwf._do_fetch_now", new_callable=lambda: lambda *a, **k: None):
        resp = await client.post("/ecmwf/fetch-now", data={"csrf_token": csrf},
                                  follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "fetching=1" in resp.headers.get("location", "")


# ── Fetch logic (mocked) ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_ecmwf_forecast_missing_package():
    """Returns None gracefully when ecmwf-opendata is not installed."""
    import sys
    with patch.dict(sys.modules, {"ecmwf.opendata": None, "ecmwf": None}):
        from importlib import import_module, reload
        import app.core.ecmwf_opendata as ecmwf_mod
        # Reload to clear cached imports
        with patch("builtins.__import__", side_effect=ImportError("no module")):
            result = await ecmwf_mod.fetch_ecmwf_forecast()
    # Returns None on import error (logged, not raised)
    # The function gracefully returns None
    assert result is None


@pytest.mark.asyncio
async def test_process_grib_returns_required_fields():
    """_process_grib returns a dict with all ForecastUpload-compatible keys."""
    import numpy as np
    from app.core.ecmwf_opendata import _process_grib

    # Build a minimal fake GRIB file using a temp NetCDF instead
    import tempfile, os
    import xarray as xr

    # Create a simple 2D precipitation array
    lats = np.array([10.0, 11.0, 12.0])
    lons = np.array([95.0, 96.0, 97.0])
    tp_data = np.array([[[0.010, 0.020, 0.030],
                          [0.015, 0.025, 0.035],
                          [0.012, 0.022, 0.032]]])  # shape (1, 3, 3) = (step, lat, lon)

    import numpy as np
    import pandas as pd

    steps = pd.to_timedelta([120], unit="h")
    ds = xr.Dataset(
        {"tp": (["step", "latitude", "longitude"], tp_data)},
        coords={
            "step": steps,
            "latitude": lats,
            "longitude": lons,
        }
    )

    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as f:
        tmp_path = f.name
    try:
        ds.to_netcdf(tmp_path)

        # Patch _open_grib to use xarray's NetCDF engine instead of cfgrib
        with patch("app.core.ecmwf_opendata._open_grib", return_value=xr.open_dataset(tmp_path)):
            result = _process_grib(
                tmp_path, "ECMWF-IFS-HRES",
                lat_min=9.0, lat_max=13.0,
                lon_min=94.0, lon_max=98.0,
                is_ensemble=False,
            )
    finally:
        os.unlink(tmp_path)

    assert result is not None
    assert "filename" in result
    assert "source" in result
    assert result["source"] == "ECMWF-IFS-HRES"
    assert "precip_mean" in result
    assert "precip_max" in result
    assert "precip_min" in result
    assert "geojson" in result
    assert "lat_min" in result
    assert "lon_min" in result
    # Values converted from metres to mm
    assert result["precip_mean"] > 1.0  # at least 10 mm (from 0.010 m * 1000)


# ── Nav ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ecmwf_link_in_nav_on_dashboard(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 200
    assert b'href="/ecmwf"' in resp.content


@pytest.mark.asyncio
async def test_ecmwf_link_in_nav_on_sync_page(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/sync", follow_redirects=False)
    assert resp.status_code == 200
    assert b'href="/ecmwf"' in resp.content


@pytest.mark.asyncio
async def test_ecmwf_nav_active_on_ecmwf_page(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/ecmwf", follow_redirects=False)
    assert resp.status_code == 200
    assert b'href="/ecmwf" class="active"' in resp.content


# ── Migration: EcmwfConfig model ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ecmwf_config_table_exists(db: AsyncSession):
    """EcmwfConfig table was created via lifespan / metadata.create_all."""
    from app.models.ecmwf_config import EcmwfConfig
    result = await db.execute(select(EcmwfConfig))
    # Just checking it doesn't throw; table exists
    result.scalars().all()


@pytest.mark.asyncio
async def test_ecmwf_config_default_values(client: AsyncClient, admin_user, db: AsyncSession):
    """First GET /ecmwf creates a singleton config with sensible defaults."""
    await _login(client)
    await client.get("/ecmwf", follow_redirects=False)

    from app.models.ecmwf_config import EcmwfConfig
    cfg = await db.scalar(select(EcmwfConfig).where(EcmwfConfig.id == 1))
    assert cfg is not None
    assert cfg.enabled is False
    assert cfg.run_time == 0
    assert cfg.sync_hour == 10
    assert cfg.lat_min == pytest.approx(0.0)
    assert cfg.lat_max == pytest.approx(35.0)
