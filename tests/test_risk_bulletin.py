"""Tests for Batches 10–12: activation heatmap, scheduled bulletin, risk gauge, risk history."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


# ── helpers ───────────────────────────────────────────────────────────────────

def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


async def _make_trigger_activation(db, lat=13.0, lon=100.0, hazard="flood"):
    from app.models.trigger import Trigger, TriggerActivation
    t = Trigger(
        name=f"HeatmapTrig-{hazard}", hazard_type=hazard,
        variable="precip_mean", operator="gt", threshold=40.0, is_active=True,
        scope_lat_min=lat - 1, scope_lat_max=lat + 1, scope_lon_min=lon - 1, scope_lon_max=lon + 1,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    act = TriggerActivation(
        trigger_id=t.id,
        value=60.0,
        status="active",
        triggered_at=datetime.now(timezone.utc),
    )
    db.add(act)
    await db.commit()
    return t, act


# ── Batch 10: activation heatmap ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alerts_page_200(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/alerts", follow_redirects=False)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_alerts_heatmap_with_data(client: AsyncClient, admin_user, db: AsyncSession):
    await _make_trigger_activation(db, lat=13.0, lon=100.0)
    await _login(client)
    resp = await client.get("/alerts?heatmap_window=30d", follow_redirects=False)
    assert resp.status_code == 200
    assert b"leaflet" in resp.content.lower() or b"map" in resp.content.lower()


@pytest.mark.asyncio
async def test_alerts_heatmap_all_windows(client: AsyncClient, admin_user, db: AsyncSession):
    await _make_trigger_activation(db)
    await _login(client)
    for window in ("30d", "90d", "1y", "all"):
        resp = await client.get(f"/alerts?heatmap_window={window}", follow_redirects=False)
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_alerts_unauthenticated_redirect(client: AsyncClient):
    resp = await client.get("/alerts", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


# ── Batch 11: scheduled bulletin ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulletin_schedule_page_admin(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/bulletin/schedule", follow_redirects=False)
    assert resp.status_code == 200
    assert b"schedule" in resp.content.lower()


@pytest.mark.asyncio
async def test_bulletin_schedule_page_non_admin(client: AsyncClient, db: AsyncSession):
    from app.core.security import hash_password
    from app.models.user import User
    u = User(email="plain@test.com", username="plain",
             hashed_password=hash_password("Plain1234"), is_active=True, role="user")
    db.add(u)
    await db.commit()
    await _login(client, "plain@test.com", "Plain1234")
    resp = await client.get("/bulletin/schedule", follow_redirects=False)
    # Non-admin redirected to /login (not a 403)
    assert resp.status_code in (302, 303)


@pytest.mark.asyncio
async def test_bulletin_subscriber_add_and_list(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.post(
        "/bulletin/subscribers/add",
        data={"email": "ops@rimes.int", "name": "Ops Team"},
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp2 = await client.get("/bulletin/schedule", follow_redirects=False)
    assert b"ops@rimes.int" in resp2.content


@pytest.mark.asyncio
async def test_bulletin_subscriber_toggle(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.bulletin_schedule import BulletinSubscriber
    sub = BulletinSubscriber(email="toggle@test.com", name="Toggle", is_active=True)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)

    await _login(client)
    resp = await client.post(
        f"/bulletin/subscribers/{sub.id}/toggle",
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(sub)
    assert sub.is_active is False


@pytest.mark.asyncio
async def test_bulletin_subscriber_delete(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.bulletin_schedule import BulletinSubscriber
    from sqlalchemy import select
    sub = BulletinSubscriber(email="del@test.com", name="Del", is_active=True)
    db.add(sub)
    await db.commit()
    sub_id = sub.id

    await _login(client)
    resp = await client.post(
        f"/bulletin/subscribers/{sub_id}/delete",
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    gone = await db.scalar(
        select(BulletinSubscriber).where(BulletinSubscriber.id == sub_id)
    )
    assert gone is None


@pytest.mark.asyncio
async def test_bulletin_send_now_no_recipients(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.post(
        "/bulletin/schedule/send-now",
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    # No recipients → redirects with flash (no SMTP attempted)
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_bulletin_send_now_delivers(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.bulletin_schedule import BulletinSubscriber
    sub = BulletinSubscriber(email="recv@test.com", name="Recv", is_active=True)
    db.add(sub)
    await db.commit()

    await _login(client)
    with patch("app.core.email.send_bulletin_email", new_callable=AsyncMock) as mock_send:
        resp = await client.post(
            "/bulletin/schedule/send-now",
            headers={"X-CSRF-Token": _csrf(client)},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_bulletin_preview_accessible(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/bulletin/generate?source=CHIRPS&days=30", follow_redirects=False)
    assert resp.status_code == 200


# ── Batch 12: composite risk score ───────────────────────────────────────────

def test_compute_risk_score_all_zero():
    from app.core.risk import compute_risk_score
    r = compute_risk_score({}, None, 0)
    assert r["total"] == 0
    assert r["level"] == "Low"
    assert r["spi_pts"] == 0
    assert r["seasonal_pts"] == 0
    assert r["trigger_pts"] == 0
    assert r["has_data"] is False


def test_compute_risk_score_spi_component():
    from app.core.risk import compute_risk_score
    # SPI-6 = -2.5 → 40 pts
    r = compute_risk_score({6: {"spi": -2.5, "label": "Extreme", "colour": "#7f1d1d"}}, None, 0)
    assert r["spi_pts"] == 40
    assert r["worst_spi"] == -2.5

    # SPI = -1.2 → 20 pts
    r2 = compute_risk_score({1: {"spi": -1.2, "label": "Moderate", "colour": "#f59e0b"}}, None, 0)
    assert r2["spi_pts"] == 20

    # SPI = 0.5 → 0 pts
    r3 = compute_risk_score({1: {"spi": 0.5, "label": "Normal", "colour": "#22c55e"}}, None, 0)
    assert r3["spi_pts"] == 0


def test_compute_risk_score_seasonal_component():
    from app.core.risk import compute_risk_score
    from unittest.mock import MagicMock
    sf = MagicMock()
    sf.below_normal_pct = 55
    r = compute_risk_score({}, sf, 0)
    assert r["seasonal_pts"] == 30

    sf.below_normal_pct = 38
    r2 = compute_risk_score({}, sf, 0)
    assert r2["seasonal_pts"] == 10

    sf.below_normal_pct = 20
    r3 = compute_risk_score({}, sf, 0)
    assert r3["seasonal_pts"] == 0


def test_compute_risk_score_trigger_component():
    from app.core.risk import compute_risk_score
    assert compute_risk_score({}, None, 3)["trigger_pts"] == 30
    assert compute_risk_score({}, None, 2)["trigger_pts"] == 20
    assert compute_risk_score({}, None, 1)["trigger_pts"] == 10
    assert compute_risk_score({}, None, 0)["trigger_pts"] == 0


def test_compute_risk_score_level_thresholds():
    from app.core.risk import compute_risk_score
    from unittest.mock import MagicMock
    sf = MagicMock()
    sf.below_normal_pct = 55  # 30 pts

    # 40+30+30 = 100 → Extreme
    r = compute_risk_score({6: {"spi": -2.5, "label": "x", "colour": "x"}}, sf, 3)
    assert r["level"] == "Extreme"
    assert r["total"] == 100

    # 40+0+0 = 40 → High? No, 40 >= 25 → Moderate
    r2 = compute_risk_score({6: {"spi": -2.5, "label": "x", "colour": "x"}}, None, 0)
    assert r2["level"] == "Moderate"
    assert r2["total"] == 40

    # 0+30+30 = 60 → High
    r3 = compute_risk_score({}, sf, 3)
    assert r3["level"] == "High"
    assert r3["total"] == 60


@pytest.mark.asyncio
async def test_drought_dashboard_200(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/drought", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Overall Risk Score" in resp.content


@pytest.mark.asyncio
async def test_dashboard_risk_widget_200(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Composite Risk" in resp.content


# ── Risk score history (Batch added now) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_record_risk_score_creates_record(db: AsyncSession):
    from sqlalchemy import select
    from app.core.risk import compute_and_record_risk_score
    from app.models.risk_history import RiskScoreRecord

    await compute_and_record_risk_score(db, source="CHIRPS")

    records = (await db.execute(select(RiskScoreRecord))).scalars().all()
    assert len(records) == 1
    assert records[0].source == "CHIRPS"
    assert 0 <= records[0].total <= 100
    assert records[0].level in ("Low", "Moderate", "High", "Extreme")


@pytest.mark.asyncio
async def test_record_risk_score_upserts_same_day(db: AsyncSession):
    from sqlalchemy import select
    from app.core.risk import compute_and_record_risk_score
    from app.models.risk_history import RiskScoreRecord

    await compute_and_record_risk_score(db, source="CHIRPS")
    await compute_and_record_risk_score(db, source="CHIRPS")

    records = (await db.execute(select(RiskScoreRecord))).scalars().all()
    assert len(records) == 1  # second call updates, not inserts


@pytest.mark.asyncio
async def test_record_risk_score_separate_sources(db: AsyncSession):
    from sqlalchemy import select
    from app.core.risk import compute_and_record_risk_score
    from app.models.risk_history import RiskScoreRecord

    await compute_and_record_risk_score(db, source="CHIRPS")
    await compute_and_record_risk_score(db, source="PERSIANN")

    records = (await db.execute(select(RiskScoreRecord))).scalars().all()
    assert len(records) == 2
    sources = {r.source for r in records}
    assert sources == {"CHIRPS", "PERSIANN"}


@pytest.mark.asyncio
async def test_drought_dashboard_shows_sparkline_with_history(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.risk_history import RiskScoreRecord
    for i in range(5):
        db.add(RiskScoreRecord(
            scored_at=datetime.now(timezone.utc) - timedelta(days=4 - i),
            source="CHIRPS", total=20 + i * 5, level="Low",
            spi_pts=0, seasonal_pts=0, trigger_pts=0,
        ))
    await db.commit()

    await _login(client)
    resp = await client.get("/drought", follow_redirects=False)
    assert resp.status_code == 200
    assert b"riskSparkline" in resp.content


# ── ROC / performance diagram ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_dashboard_200(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/observed/verify/dashboard", follow_redirects=False)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_verify_dashboard_perf_diagram_with_data(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.observed_rainfall import ObservedRainfall
    from app.models.trigger import Trigger, TriggerActivation
    from app.models.forecast import ForecastUpload

    # Activation triggered today + obs for today → they match in obs_by_date lookup
    today_dt = datetime.now(timezone.utc)
    today_date = today_dt.date()

    fc = ForecastUpload(
        filename="t.nc", source="manual", uploaded_at=today_dt - timedelta(days=2),
        lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
        time_start="2026-01-01", time_end="2026-01-15", time_steps=15,
        precip_min=5.0, precip_max=80.0, precip_mean=55.0, geojson="{}",
    )
    db.add(fc)
    obs = ObservedRainfall(
        obs_date=today_date, precip_mean=60.0, precip_max=80.0, precip_min=40.0,
        source="CHIRPS", pixel_count=100, wet_fraction=0.8,
        lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
    )
    db.add(obs)
    trig = Trigger(name="PerfTrig", hazard_type="flood",
                   variable="precip_mean", operator="gt", threshold=50.0, is_active=True)
    db.add(trig)
    await db.commit()
    await db.refresh(trig)

    # triggered_at = today so act_date matches obs_date
    act = TriggerActivation(
        trigger_id=trig.id, forecast_id=fc.id, value=55.0,
        status="acknowledged", triggered_at=today_dt,
    )
    db.add(act)
    await db.commit()

    await _login(client)
    resp = await client.get("/observed/verify/dashboard?days=7", follow_redirects=False)
    assert resp.status_code == 200
    assert b"perfChart" in resp.content
