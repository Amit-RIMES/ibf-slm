"""Tests for Batch 19: CDS ecosystem integrations (SEAS5, ERA5, GloFAS, multi-param ECMWF)."""
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


# ── CDS config page access ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cds_page_requires_auth(client: AsyncClient, db: AsyncSession):
    resp = await client.get("/cds", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_cds_page_admin_only(client: AsyncClient, db: AsyncSession):
    from app.core.security import hash_password
    from app.models.user import User
    viewer = User(email="viewer_cds@test.com", username="viewer_cds",
                  hashed_password=hash_password("Viewer1234"),
                  is_active=True, role="viewer")
    db.add(viewer)
    await db.commit()

    await _login(client, email="viewer_cds@test.com", password="Viewer1234")
    resp = await client.get("/cds", follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_cds_page_loads_for_admin(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/cds", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Copernicus Data Store" in resp.content


@pytest.mark.asyncio
async def test_cds_page_shows_config_form(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/cds", follow_redirects=False)
    assert b"api_key" in resp.content
    assert b"seas5_enabled" in resp.content
    assert b"era5_enabled" in resp.content
    assert b"glofas_enabled" in resp.content


# ── CDS config save ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cds_config_saves_settings(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    csrf = _csrf(client)
    resp = await client.post("/cds/config", data={
        "csrf_token": csrf,
        "api_key": "test-api-key-12345",
        "api_url": "https://cds.climate.copernicus.eu/api/v2",
        "lat_min": "5.0",
        "lat_max": "30.0",
        "lon_min": "90.0",
        "lon_max": "110.0",
        "seas5_sync_hour": "7",
        "seas5_sync_minute": "30",
        "seas5_lead_months": "4",
        "era5_sync_hour": "9",
        "era5_sync_minute": "0",
        "era5_lookback_days": "60",
        "glofas_sync_hour": "11",
        "glofas_sync_minute": "0",
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    from app.models.cds_config import CdsConfig
    cfg = await db.scalar(select(CdsConfig).where(CdsConfig.id == 1))
    assert cfg is not None
    assert cfg.api_key == "test-api-key-12345"
    assert cfg.lat_min == pytest.approx(5.0)
    assert cfg.seas5_lead_months == 4
    assert cfg.era5_lookback_days == 60


@pytest.mark.asyncio
async def test_cds_config_enables_seas5(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    csrf = _csrf(client)
    resp = await client.post("/cds/config", data={
        "csrf_token": csrf,
        "seas5_enabled": "on",
        "seas5_sync_hour": "8", "seas5_sync_minute": "0", "seas5_lead_months": "6",
        "era5_sync_hour": "9", "era5_sync_minute": "0", "era5_lookback_days": "30",
        "glofas_sync_hour": "11", "glofas_sync_minute": "0",
        "lat_min": "0.0", "lat_max": "35.0", "lon_min": "60.0", "lon_max": "155.0",
        "api_url": "https://cds.climate.copernicus.eu/api/v2",
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    from app.models.cds_config import CdsConfig
    cfg = await db.scalar(select(CdsConfig).where(CdsConfig.id == 1))
    assert cfg.seas5_enabled is True
    assert cfg.era5_enabled is False
    assert cfg.glofas_enabled is False


@pytest.mark.asyncio
async def test_cds_config_table_exists(db: AsyncSession):
    from app.models.cds_config import CdsConfig
    result = await db.execute(select(CdsConfig))
    result.scalars().all()


@pytest.mark.asyncio
async def test_cds_config_default_values(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    await client.get("/cds", follow_redirects=False)
    from app.models.cds_config import CdsConfig
    cfg = await db.scalar(select(CdsConfig).where(CdsConfig.id == 1))
    assert cfg is not None
    assert cfg.seas5_enabled is False
    assert cfg.era5_enabled is False
    assert cfg.glofas_enabled is False
    assert cfg.lat_min == pytest.approx(0.0)
    assert "cds.climate.copernicus.eu" in cfg.api_url


# ── GloFAS page ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_glofas_page_requires_auth(client: AsyncClient, db: AsyncSession):
    resp = await client.get("/glofas", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_glofas_page_loads_logged_in(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/glofas", follow_redirects=False)
    assert resp.status_code == 200
    assert b"GloFAS" in resp.content


@pytest.mark.asyncio
async def test_glofas_page_shows_empty_message(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/glofas", follow_redirects=False)
    assert b"No GloFAS" in resp.content


@pytest.mark.asyncio
async def test_glofas_page_shows_records(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.glofas import GlofasRecord
    rec = GlofasRecord(
        forecast_date=date(2026, 6, 16),
        source="GloFAS-v4",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=0.0, lat_max=35.0, lon_min=60.0, lon_max=155.0,
        discharge_min=0.5, discharge_max=15000.0, discharge_mean=480.2,
        lead_days=10,
        geojson='{"type":"FeatureCollection","features":[]}',
    )
    db.add(rec)
    await db.commit()

    await _login(client)
    resp = await client.get("/glofas", follow_redirects=False)
    assert resp.status_code == 200
    assert b"480.2" in resp.content
    assert b"GloFAS-v4" in resp.content


@pytest.mark.asyncio
async def test_glofas_record_table_exists(db: AsyncSession):
    from app.models.glofas import GlofasRecord
    result = await db.execute(select(GlofasRecord))
    result.scalars().all()


# ── Fetch-now endpoints ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_seas5_now_requires_admin(client: AsyncClient, db: AsyncSession):
    from app.core.security import hash_password
    from app.models.user import User
    viewer = User(email="viewer_seas5@test.com", username="viewer_seas5",
                  hashed_password=hash_password("Viewer1234"),
                  is_active=True, role="viewer")
    db.add(viewer)
    await db.commit()
    await _login(client, email="viewer_seas5@test.com", password="Viewer1234")
    csrf = _csrf(client)
    resp = await client.post("/cds/fetch-seas5", data={"csrf_token": csrf},
                              follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_fetch_seas5_now_redirects(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    csrf = _csrf(client)
    with patch("app.routers.cds._do_fetch_seas5", new_callable=lambda: lambda *a, **k: None):
        resp = await client.post("/cds/fetch-seas5", data={"csrf_token": csrf},
                                  follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "fetching=seas5" in resp.headers.get("location", "")


@pytest.mark.asyncio
async def test_fetch_era5_now_redirects(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    csrf = _csrf(client)
    with patch("app.routers.cds._do_fetch_era5", new_callable=lambda: lambda *a, **k: None):
        resp = await client.post("/cds/fetch-era5", data={"csrf_token": csrf},
                                  follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "fetching=era5" in resp.headers.get("location", "")


@pytest.mark.asyncio
async def test_fetch_glofas_now_redirects(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    csrf = _csrf(client)
    with patch("app.routers.cds._do_fetch_glofas", new_callable=lambda: lambda *a, **k: None):
        resp = await client.post("/cds/fetch-glofas", data={"csrf_token": csrf},
                                  follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "fetching=glofas" in resp.headers.get("location", "")


# ── ECMWF multi-param ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_forecast_upload_has_variable_field(db: AsyncSession):
    from app.models.forecast import ForecastUpload
    fc = ForecastUpload(
        filename="ecmwf_ifs_hres_2t_20260616.grib2",
        source="ECMWF-IFS-HRES",
        variable="2t",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=0.0, lat_max=35.0, lon_min=60.0, lon_max=155.0,
        time_start="T+024h", time_end="T+240h", time_steps=10,
        precip_min=15.2, precip_max=42.1, precip_mean=28.5,
        geojson='{"type":"FeatureCollection","features":[]}',
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)
    assert fc.variable == "2t"


@pytest.mark.asyncio
async def test_ecmwf_config_has_parameters_field(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    csrf = _csrf(client)
    resp = await client.post("/ecmwf/config", data={
        "csrf_token": csrf,
        "parameters": ["tp", "2t"],
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
    import json
    cfg = await db.scalar(select(EcmwfConfig).where(EcmwfConfig.id == 1))
    assert cfg is not None
    params = json.loads(cfg.parameters)
    assert "tp" in params
    assert "2t" in params


@pytest.mark.asyncio
async def test_ecmwf_config_shows_parameters_checkboxes(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/ecmwf", follow_redirects=False)
    assert resp.status_code == 200
    content = resp.text
    assert "Precipitation (tp)" in content
    assert "Temperature 2m" in content
    assert "Wind Speed 10m" in content
    assert "Sea Level Pressure" in content


@pytest.mark.asyncio
async def test_fetch_ecmwf_multivar_tp(monkeypatch):
    """fetch_ecmwf_forecast with variable='tp' still works (unchanged path)."""
    import app.core.ecmwf_opendata as mod

    async def fake_fetch(*args, **kwargs):
        return None
    # Just verify the function accepts variable= kwarg without error
    result = await mod.fetch_ecmwf_forecast(variable="tp")
    # Will be None because ecmwf.opendata isn't available in test env — that's fine


@pytest.mark.asyncio
async def test_fetch_ecmwf_unknown_variable():
    import app.core.ecmwf_opendata as mod
    result = await mod.fetch_ecmwf_forecast(variable="unknown_var")
    assert result is None


# ── SEAS5 core module ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_seas5_returns_empty_without_key():
    from app.core.seas5 import fetch_seas5
    result = await fetch_seas5(api_url="http://example.com", api_key="")
    assert result == []


@pytest.mark.asyncio
async def test_era5_returns_empty_without_key():
    from app.core.era5 import fetch_era5
    result = await fetch_era5(api_url="http://example.com", api_key="")
    assert result == []


@pytest.mark.asyncio
async def test_glofas_returns_none_without_key():
    from app.core.glofas_fetch import fetch_glofas
    result = await fetch_glofas(api_url="http://example.com", api_key="")
    assert result is None


@pytest.mark.asyncio
async def test_seas5_returns_empty_when_cdsapi_missing():
    from app.core.seas5 import fetch_seas5
    import sys
    with patch.dict(sys.modules, {"cdsapi": None}):
        result = await fetch_seas5(api_url="http://x.com", api_key="some-key")
    assert result == []


# ── Nav ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cds_link_in_nav_on_dashboard(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 200
    assert b'href="/cds"' in resp.content


@pytest.mark.asyncio
async def test_glofas_link_in_nav_on_dashboard(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/dashboard", follow_redirects=False)
    assert b'href="/glofas"' in resp.content


@pytest.mark.asyncio
async def test_cds_nav_active_on_cds_page(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/cds", follow_redirects=False)
    assert b'href="/cds" class="active"' in resp.content


@pytest.mark.asyncio
async def test_glofas_nav_active_on_glofas_page(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/glofas", follow_redirects=False)
    assert b'href="/glofas" class="active"' in resp.content


# ── Forecast detail shows variable units ─────────────────────────────────────

@pytest.mark.asyncio
async def test_forecast_detail_shows_celsius_for_2t(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.forecast import ForecastUpload
    fc = ForecastUpload(
        filename="ecmwf_ifs_hres_2t_20260616_100000.grib2",
        source="ECMWF-IFS-HRES",
        variable="2t",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=0.0, lat_max=35.0, lon_min=60.0, lon_max=155.0,
        time_start="T+024h", time_end="T+240h", time_steps=10,
        precip_min=18.5, precip_max=38.2, precip_mean=27.3,
        geojson='{"type":"FeatureCollection","features":[]}',
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)

    await _login(client)
    resp = await client.get(f"/forecasts/{fc.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert b"\xc2\xb0C" in resp.content  # UTF-8 encoding of °C
    assert b"Temperature" in resp.content


@pytest.mark.asyncio
async def test_forecast_detail_shows_mm_for_default(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.forecast import ForecastUpload
    fc = ForecastUpload(
        filename="ecmwf_ifs_hres_tp_default.grib2",
        source="ECMWF-IFS-HRES",
        variable=None,  # legacy null = tp
        uploaded_at=datetime.now(timezone.utc),
        lat_min=0.0, lat_max=35.0, lon_min=60.0, lon_max=155.0,
        time_start="T+024h", time_end="T+240h", time_steps=10,
        precip_min=0.0, precip_max=120.0, precip_mean=35.5,
        geojson='{"type":"FeatureCollection","features":[]}',
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)

    await _login(client)
    resp = await client.get(f"/forecasts/{fc.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert b"mm" in resp.content
    assert b"Precipitation" in resp.content
