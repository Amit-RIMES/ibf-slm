"""Tests for trigger cooldown, activation comments, and bulk acknowledge."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


# ── helpers ───────────────────────────────────────────────────────────────────

async def _make_forecast(db, precip_mean=50.0):
    from app.models.forecast import ForecastUpload
    fc = ForecastUpload(
        filename="test.nc", source="manual",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
        time_start="2026-01-01", time_end="2026-01-15", time_steps=15,
        precip_min=5.0, precip_max=precip_mean * 2, precip_mean=precip_mean,
        geojson="{}",
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)
    return fc


async def _make_trigger(db, threshold=40.0):
    from app.models.trigger import Trigger
    t = Trigger(name="CooldownTest", hazard_type="flood",
                variable="precip_mean", operator="gt", threshold=threshold, is_active=True)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def _make_activation(db, trigger, forecast, minutes_ago=0, status="active"):
    from app.models.trigger import TriggerActivation
    act = TriggerActivation(
        trigger_id=trigger.id,
        forecast_id=forecast.id,
        value=forecast.precip_mean,
        status=status,
        triggered_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    db.add(act)
    await db.commit()
    await db.refresh(act)
    return act


def _email_patches():
    return (
        patch("app.routers.triggers.send_trigger_activation_email", new_callable=AsyncMock),
        patch("app.routers.triggers.send_webhook_notifications", new_callable=AsyncMock),
        patch("app.routers.triggers.send_subscriber_alert_emails", new_callable=AsyncMock),
    )


async def _eval(fc, db):
    p1, p2, p3 = _email_patches()
    with p1, p2, p3:
        from app.routers.triggers import evaluate_triggers
        return await evaluate_triggers(fc, db)


def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


# ── trigger cooldown ──────────────────────────────────────────────────────────

async def test_cooldown_still_records_activation(db: AsyncSession):
    """Both activations are persisted even when the second is in the cooldown window."""
    from sqlalchemy import select
    from app.models.trigger import TriggerActivation

    fc1 = await _make_forecast(db, 50.0)
    trig = await _make_trigger(db, 40.0)
    await _eval(fc1, db)

    fc2 = await _make_forecast(db, 55.0)
    await _eval(fc2, db)

    result = await db.execute(
        select(TriggerActivation).where(TriggerActivation.trigger_id == trig.id)
    )
    assert len(result.scalars().all()) == 2


async def test_cooldown_suppresses_notification(db: AsyncSession):
    """Email is sent for the first activation but suppressed for the second (same cooldown window)."""
    from app.routers.triggers import evaluate_triggers

    fc1 = await _make_forecast(db, 50.0)
    await _make_trigger(db, 40.0)

    with patch("app.routers.triggers.send_trigger_activation_email", new_callable=AsyncMock) as m1, \
         patch("app.routers.triggers.send_webhook_notifications", new_callable=AsyncMock), \
         patch("app.routers.triggers.send_subscriber_alert_emails", new_callable=AsyncMock):
        await evaluate_triggers(fc1, db)
    assert m1.call_count == 1

    fc2 = await _make_forecast(db, 55.0)
    with patch("app.routers.triggers.send_trigger_activation_email", new_callable=AsyncMock) as m2, \
         patch("app.routers.triggers.send_webhook_notifications", new_callable=AsyncMock), \
         patch("app.routers.triggers.send_subscriber_alert_emails", new_callable=AsyncMock):
        await evaluate_triggers(fc2, db)
    assert m2.call_count == 0


# ── activation comments ───────────────────────────────────────────────────────

async def test_add_comment(client: AsyncClient, admin_user, db: AsyncSession):
    fc = await _make_forecast(db)
    trig = await _make_trigger(db)
    await _eval(fc, db)

    from sqlalchemy import select
    from app.models.trigger import TriggerActivation
    act = (await db.execute(
        select(TriggerActivation).where(TriggerActivation.trigger_id == trig.id)
    )).scalars().first()
    assert act is not None

    await _login(client)
    resp = await client.post(
        f"/triggers/activations/{act.id}/comments",
        data={"text": "Confirmed rainfall event."},
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from app.models.activation_comment import ActivationComment
    cmts = (await db.execute(
        select(ActivationComment).where(ActivationComment.activation_id == act.id)
    )).scalars().all()
    assert len(cmts) == 1
    assert cmts[0].text == "Confirmed rainfall event."


async def test_delete_own_comment(client: AsyncClient, admin_user, db: AsyncSession):
    fc = await _make_forecast(db)
    trig = await _make_trigger(db)
    await _eval(fc, db)

    from sqlalchemy import select
    from app.models.trigger import TriggerActivation
    act = (await db.execute(
        select(TriggerActivation).where(TriggerActivation.trigger_id == trig.id)
    )).scalars().first()
    assert act is not None

    await _login(client)

    # Add comment
    await client.post(
        f"/triggers/activations/{act.id}/comments",
        data={"text": "To be deleted."},
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )

    from app.models.activation_comment import ActivationComment
    cmt = (await db.execute(
        select(ActivationComment).where(ActivationComment.activation_id == act.id)
    )).scalars().first()
    assert cmt is not None

    # Delete it
    resp = await client.post(
        f"/triggers/activations/{act.id}/comments/{cmt.id}/delete",
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    remaining = (await db.execute(
        select(ActivationComment).where(ActivationComment.id == cmt.id)
    )).scalar_one_or_none()
    assert remaining is None


async def test_cannot_delete_other_users_comment(client: AsyncClient, admin_user, db: AsyncSession):
    """A non-admin user cannot delete another user's comment."""
    from app.core.security import hash_password
    from app.models.user import User
    other = User(email="other@test.com", username="other",
                 hashed_password=hash_password("Other1234"), is_active=True, role="user")
    db.add(other)
    await db.commit()

    fc = await _make_forecast(db)
    trig = await _make_trigger(db)
    await _eval(fc, db)

    from sqlalchemy import select
    from app.models.trigger import TriggerActivation
    act = (await db.execute(
        select(TriggerActivation).where(TriggerActivation.trigger_id == trig.id)
    )).scalars().first()

    # Admin adds a comment
    await _login(client)
    await client.post(
        f"/triggers/activations/{act.id}/comments",
        data={"text": "Admin comment."},
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )

    from app.models.activation_comment import ActivationComment
    cmt = (await db.execute(
        select(ActivationComment).where(ActivationComment.activation_id == act.id)
    )).scalars().first()
    assert cmt is not None

    # Switch to non-admin user, try to delete admin's comment
    await client.get("/logout")
    await _login(client, "other@test.com", "Other1234")
    resp = await client.post(
        f"/triggers/activations/{act.id}/comments/{cmt.id}/delete",
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    # Should redirect (not delete the comment)
    assert resp.status_code in (303, 307)

    still_there = (await db.execute(
        select(ActivationComment).where(ActivationComment.id == cmt.id)
    )).scalar_one_or_none()
    assert still_there is not None


# ── bulk acknowledge ──────────────────────────────────────────────────────────

async def test_bulk_acknowledge_success(client: AsyncClient, admin_user, db: AsyncSession):
    fc = await _make_forecast(db)
    trig = await _make_trigger(db)

    act1 = await _make_activation(db, trig, fc, minutes_ago=60)
    act2 = await _make_activation(db, trig, fc, minutes_ago=30)

    await _login(client)
    resp = await client.post(
        "/triggers/activations/bulk-acknowledge",
        data={
            "activation_ids": [str(act1.id), str(act2.id)],
            "notes": "Bulk reviewed",
        },
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Save IDs before expire_all() — accessing .id on an expired async object
    # triggers a synchronous lazy-load which fails in async context.
    ids = [act1.id, act2.id]
    # Expire the identity map so the following SELECTs re-read from the DB
    # instead of returning the stale status='active' cached objects.
    db.expire_all()
    from sqlalchemy import select
    from app.models.trigger import TriggerActivation
    for act_id in ids:
        fresh = (await db.execute(
            select(TriggerActivation).where(TriggerActivation.id == act_id)
        )).scalar_one()
        assert fresh.status == "acknowledged"
        assert fresh.notes == "Bulk reviewed"


async def test_bulk_acknowledge_requires_admin(client: AsyncClient, db: AsyncSession):
    from app.core.security import hash_password
    from app.models.user import User
    regular = User(email="reg@test.com", username="reg",
                   hashed_password=hash_password("Reg12345"), is_active=True, role="user")
    db.add(regular)
    await db.commit()

    await _login(client, "reg@test.com", "Reg12345")
    resp = await client.post(
        "/triggers/activations/bulk-acknowledge",
        data={"activation_ids": ["1"]},
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    # Non-admin gets redirected (not served)
    assert resp.status_code in (303, 307)
