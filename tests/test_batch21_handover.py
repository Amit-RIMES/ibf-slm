"""Tests for #10: Shift handover summary page."""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


@pytest.mark.asyncio
async def test_handover_requires_auth(client: AsyncClient, db: AsyncSession):
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_handover_loads_for_user(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Shift Handover" in resp.content


@pytest.mark.asyncio
async def test_handover_shows_warning_level(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert resp.status_code == 200
    # Should show one of the WMO warning names
    assert (b"Green" in resp.content or b"Yellow" in resp.content
            or b"Orange" in resp.content or b"Red" in resp.content)


@pytest.mark.asyncio
async def test_handover_shows_status_section(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert b"Active triggers" in resp.content
    assert b"Activations" in resp.content
    assert b"Unverified" in resp.content


@pytest.mark.asyncio
async def test_handover_shows_duty_officer_fields(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert b"Outgoing" in resp.content or b"outgoing" in resp.content
    assert b"Incoming" in resp.content or b"incoming" in resp.content


@pytest.mark.asyncio
async def test_handover_shows_handover_notes(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert b"Handover notes" in resp.content or b"handover-notes" in resp.content


@pytest.mark.asyncio
async def test_handover_has_print_button(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert b"print" in resp.content.lower()


@pytest.mark.asyncio
async def test_handover_has_copy_text_button(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert b"Copy" in resp.content or b"copy" in resp.content.lower()


@pytest.mark.asyncio
async def test_handover_shows_no_activations_when_empty(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert b"No activations" in resp.content or b"Trigger activations" in resp.content


@pytest.mark.asyncio
async def test_handover_shows_recent_activations(
    client: AsyncClient, admin_user, db: AsyncSession
):
    from datetime import datetime, timezone
    from app.models.trigger import Trigger, TriggerActivation

    t = Trigger(
        name="Handover Test Trigger",
        hazard_type="flood",
        variable="precip_mean",
        operator="gte",
        threshold=100.0,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    a = TriggerActivation(
        trigger_id=t.id,
        value=155.0,
        status="active",
        triggered_at=datetime.now(timezone.utc),
    )
    db.add(a)
    await db.commit()

    await _login(client)
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Handover Test Trigger" in resp.content


@pytest.mark.asyncio
async def test_handover_wmo_level_from_risk_history(
    client: AsyncClient, admin_user, db: AsyncSession
):
    from datetime import datetime, timezone
    from app.models.risk_history import RiskScoreRecord

    risk = RiskScoreRecord(
        scored_at=datetime.now(timezone.utc),
        source="CHIRPS",
        total=75,
        level="High",
        spi_pts=20,
        seasonal_pts=15,
        trigger_pts=40,
        worst_spi=None,
    )
    db.add(risk)
    await db.commit()

    await _login(client)
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Orange" in resp.content


@pytest.mark.asyncio
async def test_handover_shows_latest_bulletin(
    client: AsyncClient, admin_user, db: AsyncSession
):
    from datetime import datetime, timezone
    from app.models.bulletin_draft import BulletinDraft

    draft = BulletinDraft(
        created_at=datetime.now(timezone.utc),
        title="Flood Watch Bulletin",
        status="sent",
        source="CHIRPS",
        risk_level="High",
        approved_by_id=admin_user.id,
        approved_at=datetime.now(timezone.utc),
    )
    db.add(draft)
    await db.commit()

    await _login(client)
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Flood Watch Bulletin" in resp.content


@pytest.mark.asyncio
async def test_handover_shows_no_bulletin_message(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/reports/handover", follow_redirects=False)
    assert b"bulletin" in resp.content.lower()
