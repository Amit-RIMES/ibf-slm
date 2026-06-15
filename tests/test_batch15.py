"""Tests for Batch 15: trigger quality on list, risk map, and auto-bulletin drafts."""
import pytest
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from tests.conftest import _login


def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


# ── Feature 1: Trigger quality on list ───────────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_list_shows_quality_column(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger

    db.add(Trigger(
        name="ColTrig15", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=50.0, is_active=True,
    ))
    await db.commit()

    await _login(client)
    resp = await client.get("/triggers", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Quality" in resp.content


@pytest.mark.asyncio
async def test_trigger_quality_computed_with_data(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger
    from app.models.forecast import ForecastUpload
    from app.models.impact import ImpactRecord

    t = Trigger(
        name="QualityTrig15", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=40.0, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    now = datetime.now(timezone.utc)
    # 5 forecasts: 2 fire (>40), 1 with linked impact
    for i, mean in enumerate([10.0, 20.0, 45.0, 55.0, 65.0]):
        db.add(ForecastUpload(
            filename=f"q15fc{i}.nc", source="manual",
            uploaded_at=now - timedelta(days=40 - i),
            lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
            time_start="2026-01-01", time_end="2026-01-15", time_steps=15,
            precip_min=5.0, precip_max=mean * 1.5, precip_mean=mean, geojson="{}",
        ))
    db.add(ImpactRecord(
        event_name="Flood", hazard_type="flood",
        event_date=now.date(), country="TH", affected_population=500, description="",
    ))
    await db.commit()

    await _login(client)
    resp = await client.get("/triggers", follow_redirects=False)
    assert resp.status_code == 200
    # With 3 fires and 1 impact event in window — either CSI value or "No events" shown
    assert b"CSI" in resp.content or b"No events" in resp.content


@pytest.mark.asyncio
async def test_trigger_quality_spi_shows_spi_label(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger

    t = Trigger(
        name="SPITrig15", hazard_type="drought",
        variable="spi_3", operator="lt", threshold=-1.5, is_active=True,
    )
    db.add(t)
    await db.commit()

    await _login(client)
    resp = await client.get("/triggers", follow_redirects=False)
    assert resp.status_code == 200
    assert b"SPI" in resp.content


@pytest.mark.asyncio
async def test_trigger_quality_backtest_link(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger

    t = Trigger(
        name="BacktestLinkTrig", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=50.0, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    await _login(client)
    resp = await client.get("/triggers", follow_redirects=False)
    assert resp.status_code == 200
    assert f"/triggers/{t.id}/backtest".encode() in resp.content


@pytest.mark.asyncio
async def test_compute_trigger_quality_function(db: AsyncSession):
    from app.core.performance import compute_trigger_quality
    from app.models.trigger import Trigger
    from app.models.forecast import ForecastUpload
    from app.models.impact import ImpactRecord

    t = Trigger(
        name="PerfFnTrig", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=30.0, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    now = datetime.now(timezone.utc)
    for i, v in enumerate([10.0, 50.0, 60.0]):
        db.add(ForecastUpload(
            filename=f"pf{i}.nc", source="manual",
            uploaded_at=now - timedelta(days=10 - i),
            lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
            time_start="2026-01-01", time_end="2026-01-10", time_steps=10,
            precip_min=5.0, precip_max=v * 1.2, precip_mean=v, geojson="{}",
        ))
    db.add(ImpactRecord(
        event_name="Event", hazard_type="flood",
        event_date=now.date(), country="MM", affected_population=100, description="",
    ))
    await db.commit()

    quality = await compute_trigger_quality(db, [t])
    q = quality[t.id]
    assert q["fires"] == 2   # 50.0 and 60.0 fire
    assert q["total_forecasts"] == 3


# ── Feature 2: Risk map ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_risk_map_unauthenticated(client: AsyncClient):
    resp = await client.get("/risk/map", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_risk_map_empty(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/risk/map", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Risk Map" in resp.content


@pytest.mark.asyncio
async def test_risk_map_shows_trigger_scopes(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import Trigger

    t = Trigger(
        name="MapTrig15", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=50.0, is_active=True,
        scope_lat_min=10.0, scope_lat_max=20.0,
        scope_lon_min=90.0, scope_lon_max=100.0,
    )
    db.add(t)
    await db.commit()

    await _login(client)
    resp = await client.get("/risk/map", follow_redirects=False)
    assert resp.status_code == 200
    assert b"MapTrig15" in resp.content


@pytest.mark.asyncio
async def test_risk_map_country_stats(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.impact import ImpactRecord

    for country in ["BD", "BD", "MM"]:
        db.add(ImpactRecord(
            event_name="Event", hazard_type="flood",
            event_date=datetime.now(timezone.utc).date(),
            country=country, affected_population=100, description="",
        ))
    await db.commit()

    await _login(client)
    resp = await client.get("/risk/map", follow_redirects=False)
    assert resp.status_code == 200
    assert b"BD" in resp.content
    assert b"Impact Records by Country" in resp.content


@pytest.mark.asyncio
async def test_risk_overview_has_map_link(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/risk", follow_redirects=False)
    assert resp.status_code == 200
    assert b"/risk/map" in resp.content


# ── Feature 3: Auto-bulletin drafts ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulletin_drafts_list_unauthenticated(client: AsyncClient):
    resp = await client.get("/bulletin/drafts", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_bulletin_drafts_list_empty(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/bulletin/drafts", follow_redirects=False)
    assert resp.status_code == 200
    assert b"No bulletin drafts" in resp.content


@pytest.mark.asyncio
async def test_bulletin_drafts_list_shows_draft(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.bulletin_draft import BulletinDraft

    db.add(BulletinDraft(
        created_at=datetime.now(timezone.utc),
        source="CHIRPS", risk_level="High", total_score=60,
        title="CHIRPS — High Risk Alert", status="pending",
    ))
    await db.commit()

    await _login(client)
    resp = await client.get("/bulletin/drafts", follow_redirects=False)
    assert resp.status_code == 200
    assert b"CHIRPS" in resp.content
    assert b"High" in resp.content
    assert b"pending" in resp.content.lower()


@pytest.mark.asyncio
async def test_bulletin_draft_dismiss(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.bulletin_draft import BulletinDraft

    draft = BulletinDraft(
        created_at=datetime.now(timezone.utc),
        source="CHIRPS", risk_level="High", total_score=55,
        title="Test Draft", status="pending",
    )
    db.add(draft)
    await db.commit()
    await db.refresh(draft)

    await _login(client)
    resp = await client.post(
        f"/bulletin/drafts/{draft.id}/dismiss",
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(draft)
    assert draft.status == "dismissed"


@pytest.mark.asyncio
async def test_auto_draft_created_on_high_risk(db: AsyncSession):
    from app.core.risk import compute_and_record_risk_score
    from app.models.bulletin_draft import BulletinDraft
    from app.models.trigger import TriggerActivation
    from app.models.trigger import Trigger
    from app.models.seasonal import SeasonalForecast
    from app.models.spi import SPIRecord

    # Create SPI records that will yield Extreme SPI score (worst_spi <= -2.0 → 40 pts)
    for ts in [1, 3, 6]:
        db.add(SPIRecord(
            source="TESTDRAFT", year=2026, month=6, timescale=ts,
            spi_value=-2.5, monthly_precip_mm=10.0, n_days=30,
        ))
    # Seasonal with high below-normal (30 pts)
    from datetime import date as _date
    db.add(SeasonalForecast(
        source="TESTDRAFT", issue_date=datetime.now(timezone.utc).date(),
        valid_start=_date(2026, 6, 1), valid_end=_date(2026, 8, 31),
        below_normal_pct=55.0, near_normal_pct=30.0, above_normal_pct=15.0,
    ))
    # 3 active triggers (30 pts) → total ≥ 75 → Extreme
    t = Trigger(name="DraftTrig", hazard_type="drought",
                variable="spi_3", operator="lt", threshold=-1.5, is_active=True)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    for _ in range(3):
        db.add(TriggerActivation(
            trigger_id=t.id, status="active",
            triggered_at=datetime.now(timezone.utc),
            value=-2.0,
        ))
    await db.commit()

    await compute_and_record_risk_score(db, "TESTDRAFT")

    draft = await db.scalar(
        select(BulletinDraft).where(
            BulletinDraft.source == "TESTDRAFT",
            BulletinDraft.status == "pending",
        )
    )
    assert draft is not None
    assert draft.risk_level in ("High", "Extreme")


@pytest.mark.asyncio
async def test_auto_draft_not_duplicated_same_day(db: AsyncSession):
    from app.core.risk import compute_and_record_risk_score
    from app.models.bulletin_draft import BulletinDraft
    from app.models.trigger import TriggerActivation
    from app.models.trigger import Trigger
    from app.models.spi import SPIRecord
    from sqlalchemy import func

    for ts in [1, 3, 6]:
        db.add(SPIRecord(
            source="TESTDUP", year=2026, month=6, timescale=ts,
            spi_value=-2.5, monthly_precip_mm=10.0, n_days=30,
        ))
    t2 = Trigger(name="DupTrig", hazard_type="drought",
                 variable="spi_3", operator="lt", threshold=-1.5, is_active=True)
    db.add(t2)
    await db.commit()
    await db.refresh(t2)
    for _ in range(3):
        db.add(TriggerActivation(
            trigger_id=t2.id, status="active",
            triggered_at=datetime.now(timezone.utc),
            value=-2.0,
        ))
    await db.commit()

    # Call twice
    await compute_and_record_risk_score(db, "TESTDUP")
    await compute_and_record_risk_score(db, "TESTDUP")

    count = await db.scalar(
        select(func.count()).select_from(BulletinDraft).where(
            BulletinDraft.source == "TESTDUP",
            BulletinDraft.status == "pending",
        )
    )
    assert count == 1


@pytest.mark.asyncio
async def test_dashboard_shows_pending_drafts_banner(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.bulletin_draft import BulletinDraft

    db.add(BulletinDraft(
        created_at=datetime.now(timezone.utc),
        source="CHIRPS", risk_level="High", total_score=60,
        title="Alert", status="pending",
    ))
    await db.commit()

    await _login(client)
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 200
    assert b"bulletin draft" in resp.content.lower()
    assert b"/bulletin/drafts" in resp.content
