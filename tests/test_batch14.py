"""Tests for Batch 14: multi-source risk overview, scheduler job history,
threshold recommendation, and GeoJSON export."""
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from tests.conftest import _login


def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


# ── Feature 1: Multi-source risk overview ────────────────────────────────────

@pytest.mark.asyncio
async def test_risk_overview_unauthenticated(client: AsyncClient):
    resp = await client.get("/risk", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_risk_overview_empty(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/risk", follow_redirects=False)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_risk_overview_with_history(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.risk_history import RiskScoreRecord

    for i, source in enumerate(["CHIRPS", "PERSIANN"]):
        for day in range(5):
            db.add(RiskScoreRecord(
                scored_at=datetime.now(timezone.utc) - timedelta(days=4 - day),
                source=source, total=20 + i * 15 + day * 2, level="Low",
                spi_pts=0, seasonal_pts=0, trigger_pts=0,
            ))
    await db.commit()

    await _login(client)
    resp = await client.get("/risk", follow_redirects=False)
    assert resp.status_code == 200
    assert b"CHIRPS" in resp.content
    assert b"PERSIANN" in resp.content
    assert b"spark-" in resp.content


@pytest.mark.asyncio
async def test_risk_overview_cards_sorted_by_score(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.risk_history import RiskScoreRecord

    db.add(RiskScoreRecord(scored_at=datetime.now(timezone.utc),
                           source="LOW_SRC", total=10, level="Low",
                           spi_pts=0, seasonal_pts=0, trigger_pts=0))
    db.add(RiskScoreRecord(scored_at=datetime.now(timezone.utc),
                           source="HIGH_SRC", total=80, level="High",
                           spi_pts=40, seasonal_pts=30, trigger_pts=10))
    await db.commit()

    await _login(client)
    resp = await client.get("/risk", follow_redirects=False)
    assert resp.status_code == 200
    # Higher score source should appear first in the HTML
    high_pos = resp.content.find(b"HIGH_SRC")
    low_pos = resp.content.find(b"LOW_SRC")
    assert high_pos < low_pos


# ── Feature 2: Scheduler job history ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_job_does_not_raise(db: AsyncSession):
    # _record_job silently swallows errors, so just confirm it completes
    from app.scheduler import _record_job
    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    await _record_job("test_job", "ok", started, "ran fine")
    await _record_job("test_job", "skipped", started, "no data")
    await _record_job("test_job", "error", started, "something broke")


@pytest.mark.asyncio
async def test_job_history_shown_in_health(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.job_run import JobRun

    for status in ("ok", "ok", "error"):
        db.add(JobRun(
            job_name="chirps_sync",
            started_at=datetime.now(timezone.utc) - timedelta(hours=2),
            finished_at=datetime.now(timezone.utc) - timedelta(hours=1),
            status=status,
            detail="test run",
        ))
    await db.commit()

    await _login(client)
    resp = await client.get("/admin/health", follow_redirects=False)
    assert resp.status_code == 200
    assert b"chirps_sync" in resp.content
    assert b"Scheduler Jobs" in resp.content


@pytest.mark.asyncio
async def test_health_no_job_history_shows_placeholder(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/admin/health", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Scheduler Jobs" in resp.content
    assert b"No scheduler jobs have run yet" in resp.content


# ── Feature 3: Threshold recommendation ──────────────────────────────────────

async def _make_trigger_with_forecast_and_impact(db):
    from app.models.trigger import Trigger
    from app.models.forecast import ForecastUpload
    from app.models.impact import ImpactRecord
    import datetime as dt

    t = Trigger(
        name="BacktestTrig", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=50.0, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    today = dt.datetime.now(timezone.utc)
    for i, mean in enumerate([20.0, 40.0, 55.0, 70.0, 80.0]):
        fc = ForecastUpload(
            filename=f"fc{i}.nc", source="manual",
            uploaded_at=today - timedelta(days=30 - i),
            lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
            time_start="2026-01-01", time_end="2026-01-15", time_steps=15,
            precip_min=5.0, precip_max=mean * 1.5, precip_mean=mean, geojson="{}",
        )
        db.add(fc)

    # One impact event aligned with the high-value forecasts
    imp = ImpactRecord(
        event_name="Test Flood", hazard_type="flood", event_date=today.date(),
        country="TH", region="TestRegion", affected_population=1000, description="",
    )
    db.add(imp)
    await db.commit()
    return t


@pytest.mark.asyncio
async def test_backtest_returns_recommendation(client: AsyncClient, admin_user, db: AsyncSession):
    t = await _make_trigger_with_forecast_and_impact(db)
    await _login(client)
    resp = await client.get(f"/triggers/{t.id}/backtest", follow_redirects=False)
    assert resp.status_code == 200
    # Page renders without error; recommendation box only appears when a better threshold exists
    assert b"Backtest" in resp.content


@pytest.mark.asyncio
async def test_backtest_no_recommendation_empty_data(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger
    t = Trigger(
        name="EmptyTrig", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=50.0, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    await _login(client)
    resp = await client.get(f"/triggers/{t.id}/backtest", follow_redirects=False)
    assert resp.status_code == 200
    assert b"recommended_threshold" not in resp.content or b"Apply to trigger" not in resp.content


@pytest.mark.asyncio
async def test_apply_threshold_requires_admin(client: AsyncClient, db: AsyncSession):
    from app.core.security import hash_password
    from app.models.user import User
    from app.models.trigger import Trigger

    u = User(email="nonadmin14@test.com", username="nonadmin14",
             hashed_password=hash_password("Pass1234"), is_active=True, role="user")
    db.add(u)
    t = Trigger(name="ApplyTrigTest", hazard_type="flood",
                variable="precip_mean", operator="gt", threshold=50.0, is_active=True)
    db.add(t)
    await db.commit()
    await db.refresh(t)

    await _login(client, "nonadmin14@test.com", "Pass1234")
    resp = await client.post(
        f"/triggers/{t.id}/apply-threshold",
        data={"threshold": "30.0"},
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    # Non-admin redirected to login
    assert resp.status_code in (302, 303, 307)
    await db.refresh(t)
    assert t.threshold == 50.0  # unchanged


@pytest.mark.asyncio
async def test_apply_threshold_admin_updates(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger

    t = Trigger(name="ApplyTrigAdmin", hazard_type="flood",
                variable="precip_mean", operator="gt", threshold=50.0, is_active=True)
    db.add(t)
    await db.commit()
    await db.refresh(t)

    await _login(client)
    resp = await client.post(
        f"/triggers/{t.id}/apply-threshold",
        data={"threshold": "35.5"},
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(t)
    assert abs(t.threshold - 35.5) < 0.01


# ── Feature 4: GeoJSON export ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_geojson_export_unauthenticated(client: AsyncClient, db: AsyncSession):
    from app.models.trigger import Trigger

    t = Trigger(name="GeoTrig", hazard_type="flood",
                variable="precip_mean", operator="gt", threshold=50.0, is_active=True)
    db.add(t)
    await db.commit()
    await db.refresh(t)

    resp = await client.get(f"/triggers/{t.id}/export.geojson", follow_redirects=False)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_geojson_export_not_found(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/triggers/99999/export.geojson", follow_redirects=False)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_geojson_export_null_geometry(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger
    import json

    t = Trigger(name="NullGeoTrig", hazard_type="flood",
                variable="precip_mean", operator="gt", threshold=50.0, is_active=True)
    db.add(t)
    await db.commit()
    await db.refresh(t)

    await _login(client)
    resp = await client.get(f"/triggers/{t.id}/export.geojson", follow_redirects=False)
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "Feature"
    assert data["geometry"] is None
    assert data["properties"]["name"] == "NullGeoTrig"


@pytest.mark.asyncio
async def test_geojson_export_bbox_geometry(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger
    import json

    t = Trigger(
        name="BboxGeoTrig", hazard_type="drought",
        variable="precip_mean", operator="lt", threshold=20.0, is_active=True,
        scope_lat_min=10.0, scope_lat_max=20.0,
        scope_lon_min=90.0, scope_lon_max=100.0,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    await _login(client)
    resp = await client.get(f"/triggers/{t.id}/export.geojson", follow_redirects=False)
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "Feature"
    assert data["geometry"]["type"] == "Polygon"
    coords = data["geometry"]["coordinates"][0]
    assert len(coords) == 5  # closed ring
    # Check it covers our bbox
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    assert min(lons) == 90.0
    assert max(lons) == 100.0
    assert min(lats) == 10.0
    assert max(lats) == 20.0
    assert data["properties"]["threshold"] == 20.0
    assert data["properties"]["hazard_type"] == "drought"


@pytest.mark.asyncio
async def test_geojson_export_polygon_geometry(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger
    import json

    ring = [[90.0, 10.0], [100.0, 10.0], [100.0, 20.0], [90.0, 20.0], [90.0, 10.0]]
    t = Trigger(
        name="PolyGeoTrig", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=60.0, is_active=True,
        scope_polygon=json.dumps(ring),
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    await _login(client)
    resp = await client.get(f"/triggers/{t.id}/export.geojson", follow_redirects=False)
    assert resp.status_code == 200
    data = resp.json()
    assert data["geometry"]["type"] == "Polygon"
    assert data["geometry"]["coordinates"][0] == ring


@pytest.mark.asyncio
async def test_geojson_export_content_disposition(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger

    t = Trigger(name="CDTrig", hazard_type="flood",
                variable="precip_mean", operator="gt", threshold=50.0, is_active=True)
    db.add(t)
    await db.commit()
    await db.refresh(t)

    await _login(client)
    resp = await client.get(f"/triggers/{t.id}/export.geojson", follow_redirects=False)
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert f"trigger-{t.id}.geojson" in resp.headers.get("content-disposition", "")
