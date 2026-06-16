"""Tests for Batch 17 Feature 2: impact verification workflow."""
import pytest
from datetime import datetime, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


async def _make_trigger_and_activation(db):
    from app.models.trigger import Trigger, TriggerActivation
    t = Trigger(
        name="VerifyTrig", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=30.0, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    act = TriggerActivation(
        trigger_id=t.id, value=55.0,
        triggered_at=datetime.now(timezone.utc),
        status="acknowledged",
    )
    db.add(act)
    await db.commit()
    await db.refresh(act)
    return t, act


# ── Sitrep page ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sitrep_shows_verification_section(client: AsyncClient, admin_user, db: AsyncSession):
    t, act = await _make_trigger_and_activation(db)
    await _login(client)
    resp = await client.get(f"/triggers/activations/{act.id}/sitrep", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Impact Verification" in resp.content
    assert b"Save verdict" in resp.content


@pytest.mark.asyncio
async def test_sitrep_shows_pending_verdict_by_default(client: AsyncClient, admin_user, db: AsyncSession):
    t, act = await _make_trigger_and_activation(db)
    await _login(client)
    resp = await client.get(f"/triggers/activations/{act.id}/sitrep", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Pending" in resp.content


# ── Verify route ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_yes(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import TriggerActivation
    t, act = await _make_trigger_and_activation(db)

    await _login(client)
    csrf = _csrf(client)
    resp = await client.post(
        f"/triggers/activations/{act.id}/verify",
        data={"verdict": "yes", "impact_notes": "Major flooding in district 3.", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(act)
    assert act.impact_verdict == "yes"
    assert act.impact_notes == "Major flooding in district 3."
    assert act.verified_at is not None


@pytest.mark.asyncio
async def test_verify_partial(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import TriggerActivation
    t, act = await _make_trigger_and_activation(db)

    await _login(client)
    csrf = _csrf(client)
    await client.post(
        f"/triggers/activations/{act.id}/verify",
        data={"verdict": "partial", "csrf_token": csrf},
        follow_redirects=False,
    )
    await db.refresh(act)
    assert act.impact_verdict == "partial"


@pytest.mark.asyncio
async def test_verify_no(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import TriggerActivation
    t, act = await _make_trigger_and_activation(db)

    await _login(client)
    csrf = _csrf(client)
    await client.post(
        f"/triggers/activations/{act.id}/verify",
        data={"verdict": "no", "impact_notes": "", "csrf_token": csrf},
        follow_redirects=False,
    )
    await db.refresh(act)
    assert act.impact_verdict == "no"
    assert act.impact_notes is None


@pytest.mark.asyncio
async def test_verify_invalid_verdict_ignored(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import TriggerActivation
    t, act = await _make_trigger_and_activation(db)

    await _login(client)
    csrf = _csrf(client)
    await client.post(
        f"/triggers/activations/{act.id}/verify",
        data={"verdict": "maybe", "csrf_token": csrf},
        follow_redirects=False,
    )
    await db.refresh(act)
    assert act.impact_verdict is None  # unchanged


@pytest.mark.asyncio
async def test_verify_redirects_to_sitrep(client: AsyncClient, admin_user, db: AsyncSession):
    t, act = await _make_trigger_and_activation(db)

    await _login(client)
    csrf = _csrf(client)
    resp = await client.post(
        f"/triggers/activations/{act.id}/verify",
        data={"verdict": "yes", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert f"/triggers/activations/{act.id}/sitrep" in resp.headers["location"]


@pytest.mark.asyncio
async def test_verify_requires_auth(client: AsyncClient, db: AsyncSession):
    from app.models.user import User
    from app.core.security import hash_password
    from app.models.trigger import Trigger, TriggerActivation

    admin = User(email="adm@t.com", username="adm",
                 hashed_password=hash_password("Admin1234"), is_active=True, role="admin")
    db.add(admin)
    await db.commit()

    t = Trigger(name="AuthTrig", hazard_type="flood",
                variable="precip_mean", operator="gt", threshold=10.0, is_active=True)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    act = TriggerActivation(trigger_id=t.id, value=50.0,
                            triggered_at=datetime.now(timezone.utc), status="active")
    db.add(act)
    await db.commit()
    await db.refresh(act)

    # No login — should redirect to /login
    resp = await client.post(
        f"/triggers/activations/{act.id}/verify",
        data={"verdict": "yes"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 307)


# ── Sitrep shows updated verdict ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sitrep_shows_confirmed_verdict(client: AsyncClient, admin_user, db: AsyncSession):
    t, act = await _make_trigger_and_activation(db)

    await _login(client)
    csrf = _csrf(client)
    await client.post(
        f"/triggers/activations/{act.id}/verify",
        data={"verdict": "yes", "impact_notes": "Flooding confirmed by field team.", "csrf_token": csrf},
        follow_redirects=True,
    )

    resp = await client.get(f"/triggers/activations/{act.id}/sitrep", follow_redirects=False)
    assert b"Impacts confirmed" in resp.content
    assert b"Flooding confirmed by field team." in resp.content


@pytest.mark.asyncio
async def test_sitrep_shows_no_impact_verdict(client: AsyncClient, admin_user, db: AsyncSession):
    t, act = await _make_trigger_and_activation(db)

    await _login(client)
    csrf = _csrf(client)
    await client.post(
        f"/triggers/activations/{act.id}/verify",
        data={"verdict": "no", "csrf_token": csrf},
        follow_redirects=True,
    )

    resp = await client.get(f"/triggers/activations/{act.id}/sitrep", follow_redirects=False)
    assert b"No impacts" in resp.content


# ── Trigger detail list shows verdict badge ───────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_detail_shows_verdict_badge(client: AsyncClient, admin_user, db: AsyncSession):
    from app.models.trigger import TriggerActivation
    t, act = await _make_trigger_and_activation(db)

    # Set verdict directly on the model
    act.impact_verdict = "partial"
    await db.commit()

    await _login(client)
    resp = await client.get(f"/triggers/{t.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Partial" in resp.content


@pytest.mark.asyncio
async def test_trigger_detail_no_badge_when_pending(client: AsyncClient, admin_user, db: AsyncSession):
    t, act = await _make_trigger_and_activation(db)
    # impact_verdict is None (pending) — no badge should appear
    await _login(client)
    resp = await client.get(f"/triggers/{t.id}", follow_redirects=False)
    assert b"No impact" not in resp.content
    assert b"verdict-yes" not in resp.content
    assert b"No impact" not in resp.content
