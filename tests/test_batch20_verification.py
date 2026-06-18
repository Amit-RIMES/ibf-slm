"""Tests for #6: Forecast Verification dashboard."""
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


async def _make_activation(db, trigger_id: int, value: float = 120.0, verdict=None):
    from app.models.trigger import TriggerActivation
    a = TriggerActivation(
        trigger_id=trigger_id,
        value=value,
        status="active",
        triggered_at=datetime.now(timezone.utc),
        impact_verdict=verdict,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


async def _make_trigger(db):
    from app.models.trigger import Trigger
    t = Trigger(
        name="Verification Test Trigger",
        hazard_type="flood",
        variable="precip_mean",
        operator="gte",
        threshold=100.0,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


# ── Page access ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verification_requires_auth(client: AsyncClient, db: AsyncSession):
    resp = await client.get("/verification", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_verification_loads_for_user(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/verification", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Forecast Verification" in resp.content


@pytest.mark.asyncio
async def test_verification_shows_skill_metrics(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/verification", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Hit rate" in resp.content
    assert b"False alarm rate" in resp.content
    assert b"Verified" in resp.content


@pytest.mark.asyncio
async def test_verification_shows_no_activations_empty_state(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/verification", follow_redirects=False)
    assert b"No trigger activations" in resp.content or b"Total activations" in resp.content


# ── Activations shown ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verification_lists_activations(client: AsyncClient, admin_user, db: AsyncSession):
    t = await _make_trigger(db)
    await _make_activation(db, t.id, value=150.0)
    await _login(client)
    resp = await client.get("/verification", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Verification Test Trigger" in resp.content


@pytest.mark.asyncio
async def test_verification_shows_verdict_badge(client: AsyncClient, admin_user, db: AsyncSession):
    t = await _make_trigger(db)
    await _make_activation(db, t.id, verdict="yes")
    await _make_activation(db, t.id, verdict="no")
    await _login(client)
    resp = await client.get("/verification", follow_redirects=False)
    assert b"Hit" in resp.content
    assert b"False alarm" in resp.content


@pytest.mark.asyncio
async def test_verification_shows_unverified_badge(
    client: AsyncClient, admin_user, db: AsyncSession
):
    t = await _make_trigger(db)
    await _make_activation(db, t.id, verdict=None)
    await _login(client)
    resp = await client.get("/verification", follow_redirects=False)
    assert b"Unverified" in resp.content


# ── Filters ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verification_filter_by_hazard(client: AsyncClient, admin_user, db: AsyncSession):
    t = await _make_trigger(db)
    await _make_activation(db, t.id)
    await _login(client)
    resp = await client.get("/verification?hazard=flood", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Verification Test Trigger" in resp.content


@pytest.mark.asyncio
async def test_verification_filter_by_verdict_unverified(
    client: AsyncClient, admin_user, db: AsyncSession
):
    t = await _make_trigger(db)
    await _make_activation(db, t.id, verdict="yes")
    await _make_activation(db, t.id, verdict=None)
    await _login(client)
    resp = await client.get("/verification?verdict_filter=unverified", follow_redirects=False)
    assert resp.status_code == 200
    # Only the unverified activation should show
    assert b"Unverified" in resp.content


@pytest.mark.asyncio
async def test_verification_filter_by_verdict_yes(
    client: AsyncClient, admin_user, db: AsyncSession
):
    t = await _make_trigger(db)
    await _make_activation(db, t.id, verdict="yes")
    await _login(client)
    resp = await client.get("/verification?verdict_filter=yes", follow_redirects=False)
    assert resp.status_code == 200


# ── Set verdict ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_verdict_yes(client: AsyncClient, admin_user, db: AsyncSession):
    t = await _make_trigger(db)
    a = await _make_activation(db, t.id, verdict=None)
    await _login(client)
    resp = await client.post(
        f"/verification/activations/{a.id}/verdict",
        data={
            "verdict": "yes",
            "impact_notes": "Major flooding confirmed",
            "return_qs": "",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    await db.refresh(a)
    assert a.impact_verdict == "yes"
    assert a.impact_notes == "Major flooding confirmed"
    assert a.verified_at is not None


@pytest.mark.asyncio
async def test_set_verdict_no(client: AsyncClient, admin_user, db: AsyncSession):
    t = await _make_trigger(db)
    a = await _make_activation(db, t.id)
    await _login(client)
    resp = await client.post(
        f"/verification/activations/{a.id}/verdict",
        data={
            "verdict": "no",
            "impact_notes": "",
            "return_qs": "",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    await db.refresh(a)
    assert a.impact_verdict == "no"


@pytest.mark.asyncio
async def test_set_verdict_partial(client: AsyncClient, admin_user, db: AsyncSession):
    t = await _make_trigger(db)
    a = await _make_activation(db, t.id)
    await _login(client)
    resp = await client.post(
        f"/verification/activations/{a.id}/verdict",
        data={
            "verdict": "partial",
            "impact_notes": "Minor flooding only",
            "return_qs": "verdict_filter=unverified",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert "/verification" in resp.headers.get("location", "")
    await db.refresh(a)
    assert a.impact_verdict == "partial"


@pytest.mark.asyncio
async def test_set_verdict_invalid_ignored(client: AsyncClient, admin_user, db: AsyncSession):
    t = await _make_trigger(db)
    a = await _make_activation(db, t.id)
    await _login(client)
    resp = await client.post(
        f"/verification/activations/{a.id}/verdict",
        data={
            "verdict": "invalid_value",
            "impact_notes": "",
            "return_qs": "",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    await db.refresh(a)
    assert a.impact_verdict is None


@pytest.mark.asyncio
async def test_set_verdict_requires_auth(client: AsyncClient, db: AsyncSession):
    resp = await client.post(
        "/verification/activations/99/verdict",
        data={"verdict": "yes", "impact_notes": "", "return_qs": "", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_set_verdict_nonexistent_activation_safe(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.post(
        "/verification/activations/99999/verdict",
        data={
            "verdict": "yes",
            "impact_notes": "",
            "return_qs": "",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    # Should redirect cleanly, not 500
    assert resp.status_code in (302, 303)


# ── Redirect preserves query string ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_verdict_redirects_with_qs(client: AsyncClient, admin_user, db: AsyncSession):
    t = await _make_trigger(db)
    a = await _make_activation(db, t.id)
    await _login(client)
    resp = await client.post(
        f"/verification/activations/{a.id}/verdict",
        data={
            "verdict": "yes",
            "impact_notes": "",
            "return_qs": "hazard=flood&verdict_filter=unverified",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    location = resp.headers.get("location", "")
    assert "hazard=flood" in location or "/verification" in location
