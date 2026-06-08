"""Tests for /observed routes."""
from datetime import date, datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


async def _make_obs(db: AsyncSession, obs_date=None, precip_mean=12.5):
    from app.models.observed_rainfall import ObservedRainfall
    obs = ObservedRainfall(
        obs_date=obs_date or date(2026, 1, 15),
        source="CHIRPS",
        lat_min=0.0, lat_max=35.0, lon_min=60.0, lon_max=155.0,
        precip_mean=precip_mean,
        precip_max=45.0,
        precip_min=0.0,
        wet_fraction=0.42,
        pixel_count=500000,
        is_preliminary=False,
        geojson='{"type":"FeatureCollection","features":[]}',
        fetched_at=datetime.now(timezone.utc),
    )
    db.add(obs)
    await db.commit()
    await db.refresh(obs)
    return obs


# ── auth guard ────────────────────────────────────────────────────────────────

async def test_observed_list_requires_auth(client: AsyncClient):
    resp = await client.get("/observed", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


async def test_observed_detail_requires_auth(client: AsyncClient, db: AsyncSession):
    obs = await _make_obs(db)
    resp = await client.get(f"/observed/{obs.id}", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


async def test_observed_verify_requires_auth(client: AsyncClient):
    resp = await client.get("/observed/verify/dashboard", follow_redirects=False)
    assert resp.status_code == 303


# ── list page ─────────────────────────────────────────────────────────────────

async def test_observed_list_empty(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/observed")
    assert resp.status_code == 200
    assert "No observations yet" in resp.text


async def test_observed_list_shows_records(client: AsyncClient, admin_user, db: AsyncSession):
    await _make_obs(db, obs_date=date(2026, 1, 10), precip_mean=18.3)
    await _make_obs(db, obs_date=date(2026, 1, 11), precip_mean=5.1)
    await _login(client)
    resp = await client.get("/observed")
    assert resp.status_code == 200
    assert "10 Jan 2026" in resp.text
    assert "11 Jan 2026" in resp.text


async def test_observed_list_date_filter(client: AsyncClient, admin_user, db: AsyncSession):
    await _make_obs(db, obs_date=date(2026, 1, 5))
    await _make_obs(db, obs_date=date(2026, 1, 20))
    await _login(client)
    resp = await client.get("/observed?date_from=2026-01-15")
    assert resp.status_code == 200
    assert "20 Jan 2026" in resp.text
    assert "05 Jan 2026" not in resp.text


# ── detail page ───────────────────────────────────────────────────────────────

async def test_observed_detail_renders(client: AsyncClient, admin_user, db: AsyncSession):
    obs = await _make_obs(db, precip_mean=12.5)
    await _login(client)
    resp = await client.get(f"/observed/{obs.id}")
    assert resp.status_code == 200
    assert "12.500" in resp.text
    assert "CHIRPS" in resp.text


async def test_observed_detail_not_found_redirects(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/observed/99999", follow_redirects=False)
    assert resp.status_code == 303


# ── verification dashboard ────────────────────────────────────────────────────

async def test_observed_verify_empty(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/observed/verify/dashboard")
    assert resp.status_code == 200
    assert "Observed days" in resp.text


async def test_observed_verify_with_data(client: AsyncClient, admin_user, db: AsyncSession):
    await _make_obs(db, obs_date=date(2026, 1, 15), precip_mean=20.0)
    await _login(client)
    resp = await client.get("/observed/verify/dashboard?days=30")
    assert resp.status_code == 200
    assert "Observed days" in resp.text


# ── sync (admin only) ─────────────────────────────────────────────────────────

async def test_observed_sync_requires_admin(client: AsyncClient, db: AsyncSession):
    from app.core.security import hash_password
    from app.models.user import User
    regular = User(email="user@test.com", username="regular",
                   hashed_password=hash_password("User1234"), is_active=True, role="user")
    db.add(regular)
    await db.commit()

    await _login(client, "user@test.com", "User1234")
    from app.core.csrf import _token_for
    csrf = _token_for(client.cookies.get("access_token", ""))
    resp = await client.post(
        "/observed/sync",
        data={"lookback_days": 1},
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403
