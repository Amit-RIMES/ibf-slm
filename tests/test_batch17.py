"""Tests for Batch 17 Feature 1: external alert recipients."""
import pytest
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


# ── Recipients management page ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recipients_page_loads(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/alerts/recipients", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Alert Recipients" in resp.content


@pytest.mark.asyncio
async def test_recipients_page_requires_auth(client: AsyncClient, db: AsyncSession):
    resp = await client.get("/alerts/recipients", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_recipients_page_admin_only(client: AsyncClient, db: AsyncSession):
    from app.core.security import hash_password
    from app.models.user import User
    viewer = User(email="viewer@test.com", username="viewer",
                  hashed_password=hash_password("Viewer1234"),
                  is_active=True, role="viewer")
    db.add(viewer)
    await db.commit()

    await _login(client, email="viewer@test.com", password="Viewer1234")
    resp = await client.get("/alerts/recipients", follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_recipients_empty_state(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/alerts/recipients", follow_redirects=False)
    assert resp.status_code == 200
    assert b"No external recipients" in resp.content


# ── Add recipient ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_recipient(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.alert_recipient import AlertRecipient

    await _login(client)
    csrf = _csrf(client)
    resp = await client.post(
        "/alerts/recipients/add",
        data={"email": "field@example.org", "name": "Field Team", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    rec = await db.scalar(select(AlertRecipient).where(AlertRecipient.email == "field@example.org"))
    assert rec is not None
    assert rec.name == "Field Team"
    assert rec.is_active is True


@pytest.mark.asyncio
async def test_add_recipient_appears_in_list(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    csrf = _csrf(client)
    await client.post(
        "/alerts/recipients/add",
        data={"email": "ops@partner.org", "name": "Ops Team", "csrf_token": csrf},
        follow_redirects=True,
    )
    resp = await client.get("/alerts/recipients", follow_redirects=False)
    assert b"ops@partner.org" in resp.content
    assert b"Ops Team" in resp.content


@pytest.mark.asyncio
async def test_add_duplicate_recipient_ignored(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.alert_recipient import AlertRecipient

    await _login(client)
    csrf = _csrf(client)
    for _ in range(2):
        await client.post(
            "/alerts/recipients/add",
            data={"email": "dup@example.org", "csrf_token": csrf},
            follow_redirects=False,
        )

    count = (await db.execute(
        select(AlertRecipient).where(AlertRecipient.email == "dup@example.org")
    )).scalars().all()
    assert len(count) == 1


# ── Toggle / delete ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_toggle_recipient(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.alert_recipient import AlertRecipient

    rec = AlertRecipient(email="toggle@test.org", name="", is_active=True)
    db.add(rec)
    await db.commit()
    await db.refresh(rec)

    await _login(client)
    csrf = _csrf(client)
    resp = await client.post(
        f"/alerts/recipients/{rec.id}/toggle",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(rec)
    assert rec.is_active is False

    # Toggle back
    await client.post(
        f"/alerts/recipients/{rec.id}/toggle",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    await db.refresh(rec)
    assert rec.is_active is True


@pytest.mark.asyncio
async def test_delete_recipient(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.alert_recipient import AlertRecipient

    rec = AlertRecipient(email="delete@test.org", name="")
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    rec_id = rec.id

    await _login(client)
    csrf = _csrf(client)
    resp = await client.post(
        f"/alerts/recipients/{rec_id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    gone = await db.scalar(select(AlertRecipient).where(AlertRecipient.id == rec_id))
    assert gone is None


# ── Activation email dispatch ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_external_recipients_fetched_on_activation(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """evaluate_triggers() should include active AlertRecipient emails alongside admins."""
    from app.models.alert_recipient import AlertRecipient
    from app.models.trigger import Trigger
    from app.routers.triggers import evaluate_triggers
    from app.models.forecast import ForecastUpload
    from unittest.mock import AsyncMock, patch

    # Seed an active recipient
    db.add(AlertRecipient(email="external@ngo.org", name="NGO Partner", is_active=True))
    db.add(AlertRecipient(email="paused@ngo.org", name="Paused", is_active=False))

    t = Trigger(
        name="ExtRecipTrig", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=10.0, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    fc = ForecastUpload(
        filename="ext_test.nc", source="manual",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
        time_start="2026-01-01", time_end="2026-01-15", time_steps=15,
        precip_min=5.0, precip_max=50.0, precip_mean=55.0, geojson="{}",
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)

    import asyncio
    captured_emails = []

    async def fake_send(emails, fired):
        captured_emails.extend(emails)

    async def fake_send_webhook(*a, **k):
        pass

    # Use real enqueue (asyncio.create_task) so tasks actually run
    with patch("app.routers.triggers.send_trigger_activation_email", side_effect=fake_send), \
         patch("app.routers.triggers.send_webhook_notifications", side_effect=fake_send_webhook):
        n = await evaluate_triggers(fc, db)
        # Flush pending tasks so fake_send has a chance to run
        await asyncio.sleep(0)

    assert n > 0, "Expected trigger to fire on precip_mean=55 > threshold=10"
    assert "external@ngo.org" in captured_emails, "Active external recipient should be emailed"
    assert "paused@ngo.org" not in captured_emails, "Paused recipient should not be emailed"


# ── Alerts page has Recipients link ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_alerts_page_has_recipients_link(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/alerts", follow_redirects=False)
    assert resp.status_code == 200
    assert b"/alerts/recipients" in resp.content
    assert b"Recipients" in resp.content
