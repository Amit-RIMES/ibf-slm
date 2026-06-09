"""Tests for ensemble utilities and probabilistic trigger evaluation."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ensemble import (
    compute_exceedance_json,
    exceedance_from_members,
    exceedance_from_percentiles,
    get_exceedance,
    percentiles_from_members,
)
from tests.conftest import _login


# ── percentiles_from_members ──────────────────────────────────────────────────

def test_percentiles_median_of_odd_list():
    stats = percentiles_from_members([1.0, 2.0, 3.0, 4.0, 5.0])
    assert stats["precip_p50"] == pytest.approx(3.0, abs=0.01)


def test_percentiles_p10_p90_spread():
    members = list(range(1, 101))  # 1..100
    stats = percentiles_from_members(members)
    assert stats["precip_p10"] == pytest.approx(10.9, abs=0.5)
    assert stats["precip_p90"] == pytest.approx(90.1, abs=0.5)
    assert stats["ensemble_size"] == 100


def test_percentiles_single_member():
    stats = percentiles_from_members([42.0])
    assert stats["precip_p50"] == pytest.approx(42.0)
    assert stats["ensemble_size"] == 1


def test_percentiles_empty():
    assert percentiles_from_members([]) == {}


# ── exceedance_from_members ───────────────────────────────────────────────────

def test_exceedance_exact_fraction():
    members = [10.0, 20.0, 30.0, 40.0, 50.0]
    exc = exceedance_from_members(members, [25.0])
    # values > 25: 30, 40, 50 → 3/5
    assert exc["25.0"] == pytest.approx(0.6, abs=0.01)


def test_exceedance_all_exceed():
    members = [50.0, 60.0, 70.0]
    exc = exceedance_from_members(members, [40.0])
    assert exc["40.0"] == pytest.approx(1.0)


def test_exceedance_none_exceed():
    members = [10.0, 15.0, 20.0]
    exc = exceedance_from_members(members, [30.0])
    assert exc["30.0"] == pytest.approx(0.0)


# ── exceedance_from_percentiles ───────────────────────────────────────────────

def test_percentile_exceedance_at_p50():
    exc = exceedance_from_percentiles(10, 20, 30, 40, 50, [30.0])
    # threshold = p50 → P(X > p50) = 0.50
    assert exc["30.0"] == pytest.approx(0.5, abs=0.01)


def test_percentile_exceedance_below_p10():
    exc = exceedance_from_percentiles(10, 20, 30, 40, 50, [5.0])
    # below p10 → almost everyone exceeds
    assert exc["5.0"] == pytest.approx(1.0, abs=0.01)


def test_percentile_exceedance_above_p90():
    exc = exceedance_from_percentiles(10, 20, 30, 40, 50, [60.0])
    # above p90 → almost nobody exceeds
    assert exc["60.0"] == pytest.approx(0.0, abs=0.01)


def test_percentile_exceedance_interpolated():
    # threshold between p25 (20) and p50 (30)
    exc = exceedance_from_percentiles(10, 20, 30, 40, 50, [25.0])
    # midpoint between 0.25 and 0.50 → cdf ≈ 0.375 → exceedance ≈ 0.625
    assert exc["25.0"] == pytest.approx(0.625, abs=0.01)


# ── compute_exceedance_json / get_exceedance ─────────────────────────────────

def test_compute_exceedance_from_members():
    members = [10.0, 20.0, 30.0, 40.0, 50.0]
    js = compute_exceedance_json([30.0], members=members)
    assert js is not None
    val = get_exceedance(js, 30.0)
    assert val == pytest.approx(0.4, abs=0.01)  # 2 of 5 exceed 30


def test_compute_exceedance_from_percentiles():
    js = compute_exceedance_json([30.0], p10=10, p25=20, p50=30, p75=40, p90=50)
    assert js is not None
    val = get_exceedance(js, 30.0)
    assert val == pytest.approx(0.5, abs=0.01)


def test_get_exceedance_missing_key():
    import json
    js = json.dumps({"50.0": 0.3})
    assert get_exceedance(js, 99.0) is None


def test_compute_exceedance_no_data():
    assert compute_exceedance_json([30.0]) is None


# ── Probabilistic trigger evaluation ─────────────────────────────────────────

async def _make_forecast_with_ensemble(db, members: list[float]):
    from app.core.ensemble import compute_exceedance_json, percentiles_from_members
    from app.models.forecast import ForecastUpload
    import json as _j
    stats = percentiles_from_members(members)
    thresholds = [30.0, 50.0]
    exc_json = compute_exceedance_json(thresholds, members=members)
    fc = ForecastUpload(
        filename="ens_test.nc", source="test",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=0.0, lat_max=35.0, lon_min=60.0, lon_max=155.0,
        time_start="2026-01-01", time_end="2026-01-15", time_steps=15,
        precip_min=stats["precip_min"], precip_max=stats["precip_max"],
        precip_mean=stats["precip_mean"],
        geojson=_j.dumps({"type": "FeatureCollection", "features": []}),
        ensemble_size=stats["ensemble_size"],
        precip_p10=stats["precip_p10"], precip_p25=stats["precip_p25"],
        precip_p50=stats["precip_p50"], precip_p75=stats["precip_p75"],
        precip_p90=stats["precip_p90"],
        exceedance_json=exc_json,
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)
    return fc


async def _make_trigger_probabilistic(db, threshold=30.0, probability_threshold=0.6):
    from app.models.trigger import Trigger
    t = Trigger(
        name="ProbTest", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=threshold,
        probability_threshold=probability_threshold,
        is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


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


async def test_probabilistic_trigger_fires_when_prob_met(db: AsyncSession):
    """Trigger with probability_threshold=0.6 fires when 80% of members exceed."""
    # 8/10 members exceed 30 → P=0.8 ≥ 0.6 → should fire
    members = [10.0, 20.0, 35.0, 40.0, 45.0, 50.0, 55.0, 60.0, 65.0, 70.0]
    fc = await _make_forecast_with_ensemble(db, members)
    await _make_trigger_probabilistic(db, threshold=30.0, probability_threshold=0.6)

    count = await _eval(fc, db)
    assert count == 1

    from sqlalchemy import select
    from app.models.trigger import TriggerActivation
    acts = (await db.execute(select(TriggerActivation))).scalars().all()
    assert len(acts) == 1
    assert acts[0].probability == pytest.approx(0.8, abs=0.01)


async def test_probabilistic_trigger_does_not_fire_when_prob_not_met(db: AsyncSession):
    """Trigger with probability_threshold=0.7 does NOT fire when only 40% exceed."""
    # 4/10 members exceed 50 → P=0.4 < 0.7 → should NOT fire
    members = [10.0, 20.0, 30.0, 40.0, 55.0, 60.0, 65.0, 70.0, 15.0, 25.0]
    fc = await _make_forecast_with_ensemble(db, members)
    await _make_trigger_probabilistic(db, threshold=50.0, probability_threshold=0.7)

    count = await _eval(fc, db)
    assert count == 0


async def test_deterministic_trigger_ignores_probability_threshold(db: AsyncSession):
    """A trigger without probability_threshold uses deterministic evaluation."""
    from app.models.trigger import Trigger
    members = [60.0] * 5 + [10.0] * 5  # mean=35, 5/10 exceed 30
    fc = await _make_forecast_with_ensemble(db, members)

    # Deterministic trigger: mean=35 > 30 → fires
    t = Trigger(
        name="DetTest", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=30.0,
        is_active=True,
    )
    db.add(t)
    await db.commit()

    count = await _eval(fc, db)
    assert count == 1


async def test_probabilistic_trigger_ignores_deterministic_forecast(db: AsyncSession):
    """Probabilistic trigger with no ensemble data falls back to deterministic."""
    from app.models.forecast import ForecastUpload
    import json as _j
    # Deterministic forecast: mean=40, no ensemble data
    fc = ForecastUpload(
        filename="det.nc", source="test",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=0.0, lat_max=35.0, lon_min=60.0, lon_max=155.0,
        time_start="2026-01-01", time_end="2026-01-10", time_steps=10,
        precip_min=30.0, precip_max=50.0, precip_mean=40.0,
        geojson=_j.dumps({"type": "FeatureCollection", "features": []}),
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)

    # Probabilistic trigger, but no ensemble data → falls back to deterministic
    await _make_trigger_probabilistic(db, threshold=30.0, probability_threshold=0.6)

    count = await _eval(fc, db)
    # Falls back: mean=40 > 30 → fires
    assert count == 1


# ── API: ensemble-stats endpoint ──────────────────────────────────────────────

async def test_api_ensemble_stats_member_values(client: AsyncClient, api_key, db: AsyncSession):
    from app.models.forecast import ForecastUpload
    import json as _j
    fc = ForecastUpload(
        filename="api_test.nc", source="test",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=0.0, lat_max=10.0, lon_min=90.0, lon_max=100.0,
        time_start="2026-01-01", time_end="2026-01-05", time_steps=5,
        precip_min=5.0, precip_max=60.0, precip_mean=30.0,
        geojson=_j.dumps({"type": "FeatureCollection", "features": []}),
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)

    members = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 25.0, 35.0]
    resp = await client.post(
        f"/api/v1/forecasts/{fc.id}/ensemble-stats",
        json={"member_values": members},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ensemble_size"] == 10
    assert data["precip_p50"] is not None


async def test_api_ensemble_stats_percentiles(client: AsyncClient, api_key, db: AsyncSession):
    from app.models.forecast import ForecastUpload
    import json as _j
    fc = ForecastUpload(
        filename="api_pct_test.nc", source="test",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=0.0, lat_max=10.0, lon_min=90.0, lon_max=100.0,
        time_start="2026-01-01", time_end="2026-01-05", time_steps=5,
        precip_min=5.0, precip_max=60.0, precip_mean=30.0,
        geojson=_j.dumps({"type": "FeatureCollection", "features": []}),
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)

    resp = await client.post(
        f"/api/v1/forecasts/{fc.id}/ensemble-stats",
        json={
            "ensemble_size": 51,
            "precip_p10": 12.0, "precip_p25": 18.0,
            "precip_p50": 27.0, "precip_p75": 36.0, "precip_p90": 47.0,
        },
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ensemble_size"] == 51
    assert data["precip_p50"] == pytest.approx(27.0)


async def test_api_ensemble_stats_missing_fields(client: AsyncClient, api_key, db: AsyncSession):
    from app.models.forecast import ForecastUpload
    import json as _j
    fc = ForecastUpload(
        filename="api_bad.nc", source="test",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=0.0, lat_max=10.0, lon_min=90.0, lon_max=100.0,
        time_start="2026-01-01", time_end="2026-01-05", time_steps=5,
        precip_min=5.0, precip_max=60.0, precip_mean=30.0,
        geojson=_j.dumps({"type": "FeatureCollection", "features": []}),
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)

    resp = await client.post(
        f"/api/v1/forecasts/{fc.id}/ensemble-stats",
        json={"ensemble_size": 10},  # missing percentile fields
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 422


async def test_api_ensemble_stats_not_found(client: AsyncClient, api_key):
    resp = await client.post(
        "/api/v1/forecasts/99999/ensemble-stats",
        json={"member_values": [10.0, 20.0, 30.0]},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 404


# ── Trigger form POST: probabilistic mode ─────────────────────────────────────

def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


async def test_create_probabilistic_trigger_via_form(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """POST /triggers/new with prob_enabled creates a trigger with probability_threshold."""
    await _login(client)
    resp = await client.post(
        "/triggers/new",
        data={
            "name": "ProbFlood",
            "hazard_type": "flood",
            "variable": "precip_mean",
            "operator": "gt",
            "threshold": "40",
            "is_active": "on",
            "prob_enabled": "on",
            "probability_threshold": "0.7",
        },
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from sqlalchemy import select
    from app.models.trigger import Trigger
    t = (await db.execute(select(Trigger).where(Trigger.name == "ProbFlood"))).scalar_one()
    assert t.probability_threshold == pytest.approx(0.7)
    assert t.threshold == pytest.approx(40.0)


async def test_create_probabilistic_trigger_invalid_prob(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Probability threshold outside (0,1] returns the form with an error."""
    await _login(client)
    resp = await client.post(
        "/triggers/new",
        data={
            "name": "BadProb",
            "hazard_type": "flood",
            "variable": "precip_mean",
            "operator": "gt",
            "threshold": "30",
            "prob_enabled": "on",
            "probability_threshold": "1.5",  # invalid
        },
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    # Returns the form page with a validation error (not a redirect)
    assert resp.status_code == 200
    assert "Probability threshold" in resp.text


async def test_edit_trigger_adds_probability_threshold(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Editing an existing deterministic trigger can make it probabilistic."""
    from app.models.trigger import Trigger
    t = Trigger(
        name="EditMe", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=30.0,
        is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    await _login(client)
    resp = await client.post(
        f"/triggers/{t.id}/edit",
        data={
            "name": "EditMe",
            "hazard_type": "flood",
            "variable": "precip_mean",
            "operator": "gt",
            "threshold": "30",
            "is_active": "on",
            "prob_enabled": "on",
            "probability_threshold": "0.65",
        },
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    t_id = t.id
    db.expire_all()
    from sqlalchemy import select
    from app.models.trigger import Trigger as T
    updated = (await db.execute(select(T).where(T.id == t_id))).scalar_one()
    assert updated.probability_threshold == pytest.approx(0.65)


async def test_edit_trigger_removes_probability_threshold(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Unchecking prob_enabled clears probability_threshold back to None."""
    from app.models.trigger import Trigger
    t = Trigger(
        name="RemoveProb", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=30.0,
        probability_threshold=0.7, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    await _login(client)
    resp = await client.post(
        f"/triggers/{t.id}/edit",
        data={
            "name": "RemoveProb",
            "hazard_type": "flood",
            "variable": "precip_mean",
            "operator": "gt",
            "threshold": "30",
            "is_active": "on",
            # prob_enabled absent → unchecked
        },
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    t_id = t.id
    db.expire_all()
    from sqlalchemy import select
    from app.models.trigger import Trigger as T
    updated = (await db.execute(select(T).where(T.id == t_id))).scalar_one()
    assert updated.probability_threshold is None


# ── _process_netcdf: ensemble dimension ───────────────────────────────────────

def _make_ensemble_netcdf(tmp_path, n_members: int = 10) -> str:
    """Write a small ensemble NetCDF file with a 'member' dimension."""
    import numpy as np
    import xarray as xr

    lats = np.array([10.0, 15.0, 20.0])
    lons = np.array([90.0, 95.0, 100.0])
    times = np.array(["2026-01-01", "2026-01-02", "2026-01-03"], dtype="datetime64")
    rng = np.random.default_rng(42)
    # shape: (member, time, lat, lon)
    data = rng.uniform(5.0, 80.0, size=(n_members, len(times), len(lats), len(lons))).astype("float32")

    ds = xr.Dataset(
        {"precipitation": (["member", "time", "lat", "lon"], data)},
        coords={
            "member": np.arange(n_members),
            "time": times,
            "lat": lats,
            "lon": lons,
        },
    )
    path = str(tmp_path / "ensemble.nc")
    ds.to_netcdf(path)
    return path


def test_process_netcdf_detects_ensemble_dimension(tmp_path):
    """_process_netcdf returns ensemble stats when a member dimension is present."""
    from app.routers.forecasts import _process_netcdf

    path = _make_ensemble_netcdf(tmp_path, n_members=10)
    meta = _process_netcdf(path)

    assert meta["ensemble_size"] == 10
    assert meta["precip_p10"] is not None
    assert meta["precip_p50"] is not None
    assert meta["precip_p90"] is not None
    # p10 < p50 < p90
    assert meta["precip_p10"] < meta["precip_p50"] < meta["precip_p90"]


def test_process_netcdf_ensemble_mean_used_for_map(tmp_path):
    """The ensemble mean (not individual members) is used for the GeoJSON map."""
    import numpy as np
    import xarray as xr

    lats = np.array([10.0, 20.0])
    lons = np.array([90.0, 100.0])
    times = np.array(["2026-01-01"], dtype="datetime64")
    # Two members with known values
    data = np.array([[[[10.0, 20.0], [30.0, 40.0]]], [[[50.0, 60.0], [70.0, 80.0]]]],
                    dtype="float32")
    ds = xr.Dataset(
        {"precipitation": (["member", "time", "lat", "lon"], data)},
        coords={"member": [0, 1], "time": times, "lat": lats, "lon": lons},
    )
    path = str(tmp_path / "two_member.nc")
    ds.to_netcdf(path)

    from app.routers.forecasts import _process_netcdf
    meta = _process_netcdf(path)

    # Overall mean of [10,20,30,40,50,60,70,80] = 45
    assert meta["precip_mean"] == pytest.approx(45.0, abs=0.5)
    assert meta["ensemble_size"] == 2


def test_process_netcdf_deterministic_has_no_ensemble_fields(tmp_path):
    """A standard (no member dim) NetCDF returns no ensemble_size key."""
    import numpy as np
    import xarray as xr

    lats = np.array([10.0, 20.0])
    lons = np.array([90.0, 100.0])
    times = np.array(["2026-01-01", "2026-01-02"], dtype="datetime64")
    data = np.ones((len(times), len(lats), len(lons)), dtype="float32") * 25.0

    ds = xr.Dataset(
        {"precipitation": (["time", "lat", "lon"], data)},
        coords={"time": times, "lat": lats, "lon": lons},
    )
    path = str(tmp_path / "deterministic.nc")
    ds.to_netcdf(path)

    from app.routers.forecasts import _process_netcdf
    meta = _process_netcdf(path)

    assert "ensemble_size" not in meta or meta.get("ensemble_size") is None
    assert meta["precip_mean"] == pytest.approx(25.0, abs=0.1)


# ── Probabilistic backtest ────────────────────────────────────────────────────

import json as _json
from datetime import date


async def _make_ens_forecast(db, threshold: float, members: list, upload_date=None):
    """ForecastUpload with full ensemble fields and exceedance pre-computed."""
    from app.core.ensemble import exceedance_from_members, percentiles_from_members
    from app.models.forecast import ForecastUpload

    stats = percentiles_from_members(members)
    exceedance = exceedance_from_members(members, [threshold])

    fc = ForecastUpload(
        filename="bt_ens.nc",
        source="test",
        uploaded_at=datetime(2026, 1, 10 if upload_date is None else upload_date, tzinfo=timezone.utc),
        lat_min=0.0, lat_max=20.0, lon_min=80.0, lon_max=100.0,
        time_start="2026-01-01", time_end="2026-01-10", time_steps=10,
        precip_min=stats["precip_min"],
        precip_max=stats["precip_max"],
        precip_mean=stats["precip_mean"],
        ensemble_size=stats["ensemble_size"],
        precip_p10=stats["precip_p10"],
        precip_p25=stats["precip_p25"],
        precip_p50=stats["precip_p50"],
        precip_p75=stats["precip_p75"],
        precip_p90=stats["precip_p90"],
        exceedance_json=_json.dumps(exceedance),
        geojson=_json.dumps({"type": "FeatureCollection", "features": []}),
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)
    return fc


async def test_backtest_deterministic_trigger_no_roc(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Deterministic trigger backtest renders without the ROC curve section."""
    from app.models.trigger import Trigger

    t = Trigger(
        name="DetBacktest", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=30.0,
        is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    await _login(client)
    resp = await client.get(f"/triggers/{t.id}/backtest", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    assert "Backtest" in body
    assert "ROC curve" not in body
    assert "Brier Score" not in body


async def test_backtest_probabilistic_shows_roc_section(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Probabilistic trigger backtest shows ROC curve section when ensemble data exists."""
    from app.models.trigger import Trigger

    t = Trigger(
        name="ProbBacktest", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=30.0,
        probability_threshold=0.6, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    # 10 members: 7 exceed 30 → exceedance ≈ 0.7, above prob_threshold 0.6
    await _make_ens_forecast(db, 30.0, [10.0, 20.0, 35.0, 40.0, 45.0, 50.0, 55.0, 60.0, 65.0, 70.0])

    await _login(client)
    resp = await client.get(f"/triggers/{t.id}/backtest", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    assert "ROC curve" in body
    assert "rocChart" in body


async def test_backtest_probabilistic_brier_score_shown(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Probabilistic backtest shows Brier Score card when ensemble forecasts are available."""
    from app.models.impact import ImpactRecord
    from app.models.trigger import Trigger

    t = Trigger(
        name="BrierTest", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=25.0,
        probability_threshold=0.5, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    fc = await _make_ens_forecast(
        db, 25.0, [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 15.0, 55.0]
    )
    # Impact event close to the forecast date → marks it as a hit
    impact = ImpactRecord(
        hazard_type="flood",
        event_name="Test flood",
        event_date=date(2026, 1, 12),
        description="test flood",
        affected_population=100,
        country="BGD",
        lat=10.0, lon=90.0,
    )
    db.add(impact)
    await db.commit()

    await _login(client)
    resp = await client.get(f"/triggers/{t.id}/backtest", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    assert "Brier Score" in body
    # roc_json embedded in page should be a valid JSON array
    assert '"pt":' in body


async def test_backtest_probabilistic_roc_current_threshold_marked(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """The current probability_threshold is flagged as is_current in embedded roc_json."""
    from app.models.trigger import Trigger

    t = Trigger(
        name="RocMark", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=20.0,
        probability_threshold=0.55, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    await _make_ens_forecast(db, 20.0, list(range(5, 105, 10)))  # 10 members

    await _login(client)
    resp = await client.get(f"/triggers/{t.id}/backtest", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    # 0.55 not in the 0.05-step sweep → should have been appended
    assert '"pt": 0.55' in body or '"pt":0.55' in body
    assert '"is_current": true' in body or '"is_current":true' in body
