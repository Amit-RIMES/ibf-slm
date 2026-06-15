"""Tests for Batch 16: activation timeline, per-country risk summary, seasonal skill scores."""
import pytest
from datetime import date, datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


# ── Feature 1: Activation timeline ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_timeline_page_loads_empty(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/alerts/timeline", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Activation Timeline" in resp.content


@pytest.mark.asyncio
async def test_timeline_shows_activations(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger, TriggerActivation

    t = Trigger(
        name="TL Trigger", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=40.0, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    now = datetime.now(timezone.utc)
    db.add(TriggerActivation(
        trigger_id=t.id, value=55.0,
        triggered_at=now - timedelta(days=5),
        status="acknowledged",
    ))
    await db.commit()

    await _login(client)
    resp = await client.get("/alerts/timeline?days=30", follow_redirects=False)
    assert resp.status_code == 200
    assert b"TL Trigger" in resp.content


@pytest.mark.asyncio
async def test_timeline_hazard_filter(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger, TriggerActivation

    t = Trigger(
        name="Drought Trig16", hazard_type="drought",
        variable="precip_mean", operator="lt", threshold=10.0, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    now = datetime.now(timezone.utc)
    db.add(TriggerActivation(
        trigger_id=t.id, value=5.0,
        triggered_at=now - timedelta(days=10),
        status="active",
    ))
    await db.commit()

    await _login(client)
    # Filter by flood — should NOT show drought activation
    resp = await client.get("/alerts/timeline?days=90&hazard=flood", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Drought Trig16" not in resp.content


@pytest.mark.asyncio
async def test_timeline_days_param_range(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    for days in [30, 90, 180, 365]:
        resp = await client.get(f"/alerts/timeline?days={days}", follow_redirects=False)
        assert resp.status_code == 200


# ── Feature 2: Per-country risk summary ──────────────────────────────────────

@pytest.mark.asyncio
async def test_country_page_loads_empty(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/risk/country", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Country Risk Summary" in resp.content


@pytest.mark.asyncio
async def test_country_page_shows_impact_data(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.impact import ImpactRecord

    db.add(ImpactRecord(
        event_name="Bangladesh Flood",
        hazard_type="flood",
        country="Bangladesh",
        event_date=date(2026, 5, 1),
        affected_population=50000,
        casualties=5,
    ))
    await db.commit()

    await _login(client)
    resp = await client.get("/risk/country", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Bangladesh" in resp.content
    assert b"50" in resp.content  # 50,000 formatted


@pytest.mark.asyncio
async def test_country_page_shows_alert_badge(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger, TriggerActivation
    from app.models.impact import ImpactRecord

    t = Trigger(
        name="AlertTrig16", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=40.0, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    now = datetime.now(timezone.utc)
    activation = TriggerActivation(
        trigger_id=t.id, value=55.0,
        triggered_at=now - timedelta(days=2),
        status="active",
    )
    db.add(activation)
    await db.commit()
    await db.refresh(activation)

    db.add(ImpactRecord(
        event_name="Nepal Flood",
        hazard_type="flood",
        country="Nepal",
        event_date=date(2026, 5, 10),
        trigger_activation_id=activation.id,
        affected_population=12000,
    ))
    await db.commit()

    await _login(client)
    resp = await client.get("/risk/country", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Nepal" in resp.content
    assert b"alert" in resp.content


@pytest.mark.asyncio
async def test_country_page_totals(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.impact import ImpactRecord

    for country, affected in [("India", 100000), ("Myanmar", 30000), ("India", 20000)]:
        db.add(ImpactRecord(
            event_name="Event",
            hazard_type="flood",
            country=country,
            event_date=date(2026, 4, 1),
            affected_population=affected,
        ))
    await db.commit()

    await _login(client)
    resp = await client.get("/risk/country", follow_redirects=False)
    assert resp.status_code == 200
    # Two distinct countries
    assert b"India" in resp.content
    assert b"Myanmar" in resp.content


# ── Feature 3: Seasonal forecast skill scores ─────────────────────────────────

@pytest.mark.asyncio
async def test_skill_page_loads_empty(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/seasonal/skill", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Skill" in resp.content


@pytest.mark.asyncio
async def test_skill_no_data_message(client: AsyncClient, admin_user, db: AsyncSession):
    """With no completed forecasts, shows empty state."""
    await _login(client)
    resp = await client.get("/seasonal/skill", follow_redirects=False)
    assert resp.status_code == 200
    assert b"No completed forecasts" in resp.content


@pytest.mark.asyncio
async def test_skill_scores_computed(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.seasonal import SeasonalForecast
    from app.models.observed_rainfall import ObservedRainfall

    # Past forecast (valid_end well in the past)
    sf = SeasonalForecast(
        source="IRI",
        issue_date=date(2024, 5, 1),
        valid_start=date(2024, 6, 1),
        valid_end=date(2024, 8, 31),
        variable="precip",
        below_normal_pct=40.0,
        near_normal_pct=35.0,
        above_normal_pct=25.0,
    )
    db.add(sf)

    # Observed rainfall covering the valid period (June–Aug 2024)
    for day_offset in range(92):
        d = date(2024, 6, 1) + timedelta(days=day_offset)
        db.add(ObservedRainfall(
            obs_date=d, source="CHIRPS",
            lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
            precip_mean=2.5, precip_max=5.0, precip_min=0.5,
            wet_fraction=0.6, pixel_count=100,
        ))

    # Additional historical obs for reference percentiles (same months in prior years)
    for yr in [2022, 2023]:
        for day_offset in range(92):
            d = date(yr, 6, 1) + timedelta(days=day_offset)
            db.add(ObservedRainfall(
                obs_date=d, source="CHIRPS",
                lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
                precip_mean=3.0, precip_max=6.0, precip_min=1.0,
                wet_fraction=0.7, pixel_count=100,
            ))
    await db.commit()

    await _login(client)
    resp = await client.get("/seasonal/skill", follow_redirects=False)
    assert resp.status_code == 200
    assert b"IRI" in resp.content
    # RPSS should appear
    assert b"RPSS" in resp.content
    # The forecast row should show "1 forecast"
    assert b"1 forecast" in resp.content


@pytest.mark.asyncio
async def test_skill_source_filter(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.seasonal import SeasonalForecast
    from app.models.observed_rainfall import ObservedRainfall

    for src in ["IRI", "ECMWF-SEAS5"]:
        db.add(SeasonalForecast(
            source=src,
            issue_date=date(2024, 5, 1),
            valid_start=date(2024, 6, 1),
            valid_end=date(2024, 6, 30),
            variable="precip",
            below_normal_pct=33.0,
            near_normal_pct=34.0,
            above_normal_pct=33.0,
        ))

    # Observed rainfall — 2024 period + historical for varied reference percentiles
    obs_values = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0,
                  5.5, 6.0, 6.5, 7.0, 1.2, 2.3, 3.7, 4.8, 0.8, 2.8,
                  3.2, 5.1, 1.8, 4.2, 0.9, 3.9, 2.1, 5.8, 1.4, 3.3]
    for day_offset in range(30):
        d = date(2024, 6, 1) + timedelta(days=day_offset)
        db.add(ObservedRainfall(
            obs_date=d, source="CHIRPS",
            lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
            precip_mean=obs_values[day_offset], precip_max=obs_values[day_offset] * 2,
            precip_min=obs_values[day_offset] * 0.2,
            wet_fraction=0.6, pixel_count=100,
        ))
    await db.commit()

    await _login(client)
    resp = await client.get("/seasonal/skill?source=IRI", follow_redirects=False)
    assert resp.status_code == 200
    assert b"IRI" in resp.content
    # Only 1 forecast row (IRI) should be scored — ECMWF-SEAS5 may appear in filter dropdown
    assert b"1 forecast" in resp.content


@pytest.mark.asyncio
async def test_skill_forecast_without_obs_skipped(client: AsyncClient, admin_user, db: AsyncSession):
    """Forecasts with no observed data for the period are silently skipped."""
    from app.models.seasonal import SeasonalForecast

    db.add(SeasonalForecast(
        source="RIMES",
        issue_date=date(2023, 1, 1),
        valid_start=date(2023, 3, 1),
        valid_end=date(2023, 5, 31),
        variable="precip",
        below_normal_pct=30.0,
        near_normal_pct=40.0,
        above_normal_pct=30.0,
    ))
    await db.commit()

    await _login(client)
    # No obs in DB at all → page should still load, showing empty state
    resp = await client.get("/seasonal/skill", follow_redirects=False)
    assert resp.status_code == 200
    assert b"No completed forecasts" in resp.content


@pytest.mark.asyncio
async def test_skill_sub_nav_present(client: AsyncClient, admin_user, db: AsyncSession):
    """Seasonal list page should now contain the Skill Scores sub-nav link."""
    await _login(client)
    resp = await client.get("/seasonal", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Skill Scores" in resp.content
    assert b"/seasonal/skill" in resp.content
