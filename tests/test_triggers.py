import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession


async def _make_forecast(db: AsyncSession, precip_mean=30.0, precip_max=80.0, precip_min=5.0):
    from app.models.forecast import ForecastUpload
    fc = ForecastUpload(
        filename="test.nc", source="manual",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
        time_start="2026-01-01", time_end="2026-01-15", time_steps=15,
        precip_min=precip_min, precip_max=precip_max, precip_mean=precip_mean,
        geojson="{}",
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)
    return fc


async def _make_trigger(db: AsyncSession, variable="precip_mean", operator="gt", threshold=25.0,
                         is_active=True, **kwargs):
    from app.models.trigger import Trigger
    t = Trigger(name="Test", hazard_type="flood", variable=variable,
                operator=operator, threshold=threshold, is_active=is_active, **kwargs)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def test_trigger_fires(db: AsyncSession):
    fc = await _make_forecast(db, precip_mean=50.0)
    trigger = await _make_trigger(db, variable="precip_mean", operator="gt", threshold=40.0)

    with patch("app.routers.triggers.send_trigger_activation_email", new_callable=AsyncMock), \
         patch("app.routers.triggers.send_webhook_notifications", new_callable=AsyncMock), \
         patch("app.routers.triggers.send_subscriber_alert_emails", new_callable=AsyncMock):
        from app.routers.triggers import evaluate_triggers
        count = await evaluate_triggers(fc, db)

    assert count == 1


async def test_trigger_does_not_fire(db: AsyncSession):
    fc = await _make_forecast(db, precip_mean=20.0)
    await _make_trigger(db, variable="precip_mean", operator="gt", threshold=40.0)

    from app.routers.triggers import evaluate_triggers
    count = await evaluate_triggers(fc, db)
    assert count == 0


async def test_inactive_trigger_skipped(db: AsyncSession):
    fc = await _make_forecast(db, precip_mean=100.0)
    await _make_trigger(db, variable="precip_mean", operator="gt", threshold=10.0, is_active=False)

    from app.routers.triggers import evaluate_triggers
    count = await evaluate_triggers(fc, db)
    assert count == 0


async def test_compound_and_both_must_fire(db: AsyncSession):
    fc = await _make_forecast(db, precip_mean=50.0, precip_max=60.0)
    # AND: mean > 40 AND max > 70 — max doesn't exceed 70
    await _make_trigger(db, variable="precip_mean", operator="gt", threshold=40.0,
                         condition_2_variable="precip_max", condition_2_operator="gt",
                         condition_2_threshold=70.0, logic_op="and")

    from app.routers.triggers import evaluate_triggers
    count = await evaluate_triggers(fc, db)
    assert count == 0


async def test_compound_and_both_fire(db: AsyncSession):
    fc = await _make_forecast(db, precip_mean=50.0, precip_max=80.0)
    # AND: mean > 40 AND max > 70 — both fire
    await _make_trigger(db, variable="precip_mean", operator="gt", threshold=40.0,
                         condition_2_variable="precip_max", condition_2_operator="gt",
                         condition_2_threshold=70.0, logic_op="and")

    with patch("app.routers.triggers.send_trigger_activation_email", new_callable=AsyncMock), \
         patch("app.routers.triggers.send_webhook_notifications", new_callable=AsyncMock), \
         patch("app.routers.triggers.send_subscriber_alert_emails", new_callable=AsyncMock):
        from app.routers.triggers import evaluate_triggers
        count = await evaluate_triggers(fc, db)
    assert count == 1


async def test_compound_or_one_fires(db: AsyncSession):
    fc = await _make_forecast(db, precip_mean=10.0, precip_max=80.0)
    # OR: mean > 40 OR max > 70 — only max fires
    await _make_trigger(db, variable="precip_mean", operator="gt", threshold=40.0,
                         condition_2_variable="precip_max", condition_2_operator="gt",
                         condition_2_threshold=70.0, logic_op="or")

    with patch("app.routers.triggers.send_trigger_activation_email", new_callable=AsyncMock), \
         patch("app.routers.triggers.send_webhook_notifications", new_callable=AsyncMock), \
         patch("app.routers.triggers.send_subscriber_alert_emails", new_callable=AsyncMock):
        from app.routers.triggers import evaluate_triggers
        count = await evaluate_triggers(fc, db)
    assert count == 1


async def test_point_in_polygon():
    from app.routers.triggers import _point_in_polygon
    # Square: (0,0) to (10,10) in lon/lat
    ring = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    assert _point_in_polygon(5, 5, ring) is True    # inside
    assert _point_in_polygon(15, 5, ring) is False  # outside
    assert _point_in_polygon(-1, 5, ring) is False  # outside


async def test_operators(db: AsyncSession):
    for op, threshold, val, should_fire in [
        ("gt", 10, 15, True), ("gt", 10, 10, False),
        ("gte", 10, 10, True), ("gte", 10, 9, False),
        ("lt", 10, 5, True), ("lt", 10, 10, False),
        ("lte", 10, 10, True), ("lte", 10, 11, False),
    ]:
        fc = await _make_forecast(db, precip_mean=val)
        await _make_trigger(db, variable="precip_mean", operator=op, threshold=threshold)

        from app.routers.triggers import evaluate_triggers
        with patch("app.routers.triggers.send_trigger_activation_email", new_callable=AsyncMock), \
             patch("app.routers.triggers.send_webhook_notifications", new_callable=AsyncMock), \
             patch("app.routers.triggers.send_subscriber_alert_emails", new_callable=AsyncMock):
            count = await evaluate_triggers(fc, db)
        assert (count == 1) == should_fire, f"op={op} threshold={threshold} val={val}"

        # Clean up activations for next iteration
        from sqlalchemy import delete
        from app.models.trigger import TriggerActivation, Trigger
        await db.execute(delete(TriggerActivation))
        await db.execute(delete(Trigger))
        await db.commit()
