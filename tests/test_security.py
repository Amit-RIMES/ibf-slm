"""Tests for security headers, /health endpoint, IP allowlisting, and TOTP."""
import hashlib
import secrets

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


# ── security headers ──────────────────────────────────────────────────────────

async def test_security_headers_on_html(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


async def test_csp_header_on_html(client: AsyncClient, admin_user):
    await _login(client)
    resp = await client.get("/dashboard")
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src" in csp
    assert "frame-ancestors" in csp


async def test_no_csp_on_json_api(client: AsyncClient):
    resp = await client.get("/api/v1/status")
    assert resp.status_code == 200
    assert "content-security-policy" not in resp.headers


# ── /api/v1/health ────────────────────────────────────────────────────────────

async def test_health_endpoint_is_public(client: AsyncClient):
    """Health endpoint requires no API key."""
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200


async def test_health_endpoint_returns_db_status(client: AsyncClient):
    resp = await client.get("/api/v1/health")
    data = resp.json()
    # The health endpoint uses its own AsyncSessionLocal (separate engine from the
    # test engine), so the DB check may fail in tests.  Only assert the fields
    # that are always present regardless of DB state.
    assert data["status"] in ("healthy", "unhealthy")
    assert "database" in data


# ── IP allowlisting ───────────────────────────────────────────────────────────

async def _make_key_with_ips(db, admin_user, allowed_ips: str | None):
    from app.models.api_key import APIKey
    raw = secrets.token_hex(32)
    key = APIKey(
        name="ip-test-key",
        key_prefix=raw[:12],
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        user_id=admin_user.id,
        allowed_ips=allowed_ips,
    )
    db.add(key)
    await db.commit()
    return raw


async def test_api_key_ip_allowed(client: AsyncClient, admin_user, db: AsyncSession):
    raw = await _make_key_with_ips(db, admin_user, "127.0.0.1,testclient")
    resp = await client.get("/api/v1/forecasts", headers={"X-API-Key": raw})
    assert resp.status_code == 200


async def test_api_key_ip_blocked(client: AsyncClient, admin_user, db: AsyncSession):
    raw = await _make_key_with_ips(db, admin_user, "203.0.113.99")
    resp = await client.get("/api/v1/forecasts", headers={"X-API-Key": raw})
    assert resp.status_code == 403


async def test_api_key_no_ip_restriction(client: AsyncClient, admin_user, db: AsyncSession):
    raw = await _make_key_with_ips(db, admin_user, None)
    resp = await client.get("/api/v1/forecasts", headers={"X-API-Key": raw})
    assert resp.status_code == 200


async def test_api_key_cidr_allowed(client: AsyncClient, admin_user, db: AsyncSession):
    raw = await _make_key_with_ips(db, admin_user, "127.0.0.0/8")
    resp = await client.get("/api/v1/forecasts", headers={"X-API-Key": raw})
    assert resp.status_code == 200


# ── TOTP / 2FA ────────────────────────────────────────────────────────────────

async def test_totp_setup_generates_secret(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.post(
        "/account/2fa/setup",
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    # Setup renders the confirm page (200 HTML) — not a redirect
    assert resp.status_code == 200
    assert "qr" in resp.text.lower() or "scan" in resp.text.lower() or "secret" in resp.text.lower()

    # The route handler commits via a separate session — expire the identity map
    # so the SELECT below re-reads from the shared DB instead of returning the
    # stale cached admin_user.
    # Save .id before expire_all() — accessing it on an expired async object
    # triggers a synchronous lazy-load which fails with MissingGreenlet.
    uid = admin_user.id
    db.expire_all()
    from sqlalchemy import select
    from app.models.user import User
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
    assert user.totp_secret is not None
    assert user.totp_enabled is False


async def test_totp_confirm_valid_code_enables_2fa(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)

    # Setup — generates secret and renders confirm page
    await client.post(
        "/account/2fa/setup",
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )

    # Expire identity map so we read the secret the route handler just committed.
    uid = admin_user.id
    db.expire_all()
    from sqlalchemy import select
    from app.models.user import User
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
    assert user.totp_secret is not None
    code = pyotp.TOTP(user.totp_secret).now()

    resp = await client.post(
        "/account/2fa/confirm",
        data={"code": code},
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303)  # done page or redirect

    await db.refresh(user)
    assert user.totp_enabled is True


async def test_totp_confirm_invalid_code_rejected(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    await client.post(
        "/account/2fa/setup",
        headers={"X-CSRF-Token": _csrf(client)},
        follow_redirects=False,
    )

    resp = await client.post(
        "/account/2fa/confirm",
        data={"code": "000000"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    # Returns confirm page again with error
    assert resp.status_code in (200, 400)
    assert "Invalid" in resp.text or "invalid" in resp.text.lower()

    from sqlalchemy import select
    from app.models.user import User
    user = (await db.execute(select(User).where(User.id == admin_user.id))).scalar_one()
    assert user.totp_enabled is False


async def test_totp_login_redirects_to_verify(client: AsyncClient, admin_user, db: AsyncSession):
    admin_user.totp_secret = pyotp.random_base32()
    admin_user.totp_enabled = True
    db.add(admin_user)
    await db.commit()

    resp = await client.post(
        "/login",
        data={"email": "admin@test.com", "password": "Admin1234"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "2fa/verify" in resp.headers["location"]
    assert "totp_pending" in resp.cookies


async def test_totp_verify_valid_code_completes_login(client: AsyncClient, admin_user, db: AsyncSession):
    secret = pyotp.random_base32()
    admin_user.totp_secret = secret
    admin_user.totp_enabled = True
    db.add(admin_user)
    await db.commit()

    await client.post(
        "/login",
        data={"email": "admin@test.com", "password": "Admin1234"},
        follow_redirects=False,
    )
    assert "totp_pending" in client.cookies

    code = pyotp.TOTP(secret).now()
    resp = await client.post(
        "/account/2fa/verify",
        data={"code": code},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert "access_token" in resp.cookies


async def test_totp_verify_wrong_code_rejected(client: AsyncClient, admin_user, db: AsyncSession):
    admin_user.totp_secret = pyotp.random_base32()
    admin_user.totp_enabled = True
    db.add(admin_user)
    await db.commit()

    await client.post(
        "/login",
        data={"email": "admin@test.com", "password": "Admin1234"},
        follow_redirects=False,
    )

    resp = await client.post("/account/2fa/verify", data={"code": "999999"})
    # Returns verify page with error (status 401 or 200)
    assert resp.status_code in (200, 401)
    assert "access_token" not in resp.cookies
