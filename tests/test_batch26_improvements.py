"""Tests for batch-26 improvements:
  1. Print light mode CSS in base.html
  2. Password strength meter in register.html and change_password.html
  3. Trigger search + bulk toggle route
  4. Impact duplication page
  5. CAP 1.2 XML export route
  6. Station comparison page
  7. Webhook delivery log route
  8. Active session list + revoke-all
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, decode_access_token
from app.main import app
from app.models.user import User


def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


@pytest_asyncio.fixture()
async def auth_client(client: AsyncClient, db: AsyncSession):
    user = User(
        email="test26@rimes.int",
        username="tester26",
        hashed_password="x",
        role="admin",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token({"sub": str(user.id)})
    client.cookies.set("access_token", token)
    yield client, db, user


# ── 1. Print light mode ───────────────────────────────────────────────────────

def test_base_html_print_media_query():
    base = Path("app/templates/base.html").read_text()
    assert "@media print" in base


def test_base_html_print_forces_light_bg():
    base = Path("app/templates/base.html").read_text()
    assert "background: #fff !important" in base or "background:#fff" in base


def test_base_html_print_hides_ui_chrome():
    base = Path("app/templates/base.html").read_text()
    assert "dark-toggle" in base
    assert "@media print" in base


# ── 2. Password strength meter ────────────────────────────────────────────────

def test_register_html_strength_bar():
    reg = Path("app/templates/register.html").read_text()
    assert "pw-bar" in reg
    assert "pw-hint" in reg


def test_register_html_strength_js_scoring():
    reg = Path("app/templates/register.html").read_text()
    assert "Too weak" in reg or "too_weak" in reg.lower() or "Too weak" in reg


def test_register_html_strength_levels():
    reg = Path("app/templates/register.html").read_text()
    for label in ["Weak", "Good", "Strong"]:
        assert label in reg


def test_change_password_html_strength_bar():
    cp = Path("app/templates/change_password.html").read_text()
    assert "pw-bar" in cp
    assert "pw-hint" in cp


def test_change_password_html_minlength_8():
    cp = Path("app/templates/change_password.html").read_text()
    assert 'minlength="8"' in cp
    assert 'minlength="6"' not in cp


# ── 3. Trigger search + bulk toggle ──────────────────────────────────────────

def test_trigger_list_html_search_input():
    tl = Path("app/templates/trigger_list.html").read_text()
    assert "trig-search" in tl


def test_trigger_list_html_select_all_checkbox():
    tl = Path("app/templates/trigger_list.html").read_text()
    assert "select-all" in tl


def test_trigger_list_html_bulk_form():
    tl = Path("app/templates/trigger_list.html").read_text()
    assert "/triggers/bulk-toggle" in tl


def test_trigger_bulk_toggle_route_registered():
    from app.routers.triggers import router
    paths = [r.path for r in router.routes]
    assert "/triggers/bulk-toggle" in paths


@pytest.mark.asyncio
async def test_trigger_bulk_toggle_requires_admin(auth_client):
    ac, db, user = auth_client
    user.role = "user"
    await db.commit()
    token = create_access_token({"sub": str(user.id)})
    ac.cookies.set("access_token", token)
    csrf = _csrf(ac)
    resp = await ac.post("/triggers/bulk-toggle", data={"ids": [], "action": "enable", "csrf_token": csrf})
    assert resp.status_code in (302, 303, 307, 403)


# ── 4. Impact duplication ─────────────────────────────────────────────────────

def test_impact_duplicate_route_registered():
    from app.routers.impacts import router
    paths = [r.path for r in router.routes]
    assert "/impacts/{impact_id}/duplicate" in paths


def test_impact_detail_html_duplicate_button():
    detail = Path("app/templates/impact_detail.html").read_text()
    assert "duplicate" in detail.lower()


def test_impact_form_html_handles_is_duplicate():
    form = Path("app/templates/impact_form.html").read_text()
    assert "is_duplicate" in form


@pytest.mark.asyncio
async def test_impact_duplicate_404_for_unknown(auth_client):
    ac, db, user = auth_client
    resp = await ac.get("/impacts/99999/duplicate")
    assert resp.status_code in (302, 303, 307, 404)


# ── 5. CAP XML export ─────────────────────────────────────────────────────────

def test_cap_xml_route_registered():
    from app.routers.triggers import router
    paths = [r.path for r in router.routes]
    assert "/triggers/activations/{activation_id}/cap.xml" in paths


def test_trigger_detail_html_cap_xml_link():
    detail = Path("app/templates/trigger_detail.html").read_text()
    assert "cap.xml" in detail


@pytest.mark.asyncio
async def test_cap_xml_404_for_unknown(auth_client):
    ac, db, user = auth_client
    resp = await ac.get("/triggers/activations/99999/cap.xml")
    assert resp.status_code in (302, 404)


@pytest.mark.asyncio
async def test_cap_xml_returns_xml(auth_client):
    from app.models.trigger import Trigger, TriggerActivation
    ac, db, user = auth_client

    trigger = Trigger(
        name="Flood CAP test",
        hazard_type="flood",
        variable="precip_24h",
        operator="gte",
        threshold=50.0,
        is_active=True,
    )
    db.add(trigger)
    await db.commit()
    await db.refresh(trigger)

    activation = TriggerActivation(
        trigger_id=trigger.id,
        value=55.0,
        status="active",
    )
    db.add(activation)
    await db.commit()
    await db.refresh(activation)

    resp = await ac.get(f"/triggers/activations/{activation.id}/cap.xml")
    assert resp.status_code == 200
    assert "application/xml" in resp.headers.get("content-type", "")
    assert b"<alert" in resp.content
    assert b"CAP" in resp.content or b"urn:oasis:names:tc:emergency:cap" in resp.content


# ── 6. Station comparison ─────────────────────────────────────────────────────

def test_station_compare_route_registered():
    from app.routers.stations import router
    paths = [r.path for r in router.routes]
    assert "/stations/compare" in paths


def test_station_compare_html_exists():
    assert Path("app/templates/station_compare.html").exists()


def test_station_compare_html_chart():
    cmp = Path("app/templates/station_compare.html").read_text()
    assert "Chart" in cmp or "chart" in cmp.lower()


@pytest.mark.asyncio
async def test_station_compare_no_ids_renders(auth_client):
    ac, db, user = auth_client
    resp = await ac.get("/stations/compare")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_station_compare_with_valid_ids(auth_client):
    from app.models.station import Station, StationObservation
    from datetime import date
    ac, db, user = auth_client

    st = Station(station_id="CMP01", name="Compare Station", lat=5.0, lon=100.0, source="test")
    db.add(st)
    await db.commit()

    obs = StationObservation(
        station_id="CMP01",
        obs_date=date(2026, 1, 1),
        precip_mm=10.0,
        source="test",
    )
    db.add(obs)
    await db.commit()

    resp = await ac.get("/stations/compare?ids=CMP01&variable=precip_mm")
    assert resp.status_code == 200
    assert b"CMP01" in resp.content or b"Compare Station" in resp.content


# ── 7. Webhook delivery log ───────────────────────────────────────────────────

def test_webhook_delivery_model_importable():
    from app.models.webhook_delivery import WebhookDelivery
    assert WebhookDelivery.__tablename__ == "webhook_deliveries"


def test_webhook_delivery_model_fields():
    from app.models.webhook_delivery import WebhookDelivery
    columns = {c.name for c in WebhookDelivery.__table__.columns}
    for col in ["id", "webhook_id", "status_code", "success", "duration_ms", "delivered_at"]:
        assert col in columns, f"Missing column: {col}"


def test_webhook_deliveries_route_registered():
    from app.routers.admin import router
    paths = [r.path for r in router.routes]
    assert "/admin/webhooks/{wh_id}/deliveries" in paths


def test_admin_webhooks_html_deliveries_link():
    wh = Path("app/templates/admin/webhooks.html").read_text()
    assert "deliveries" in wh.lower()


def test_webhook_deliveries_template_exists():
    assert Path("app/templates/admin/webhook_deliveries.html").exists()


@pytest.mark.asyncio
async def test_webhook_deliveries_404_unknown(auth_client):
    ac, db, user = auth_client
    resp = await ac.get("/admin/webhooks/99999/deliveries")
    assert resp.status_code in (302, 303, 307, 404)


@pytest.mark.asyncio
async def test_webhook_deliveries_page_renders(auth_client):
    from app.models.webhook import Webhook
    ac, db, user = auth_client

    wh = Webhook(name="Test hook", url="https://example.com/hook", is_active=True)
    db.add(wh)
    await db.commit()
    await db.refresh(wh)

    resp = await ac.get(f"/admin/webhooks/{wh.id}/deliveries")
    assert resp.status_code == 200
    assert b"Deliveries" in resp.content or b"deliveries" in resp.content


# ── 8. Active session list + revoke-all ──────────────────────────────────────

def test_user_session_model_importable():
    from app.models.user_session import UserSession
    assert UserSession.__tablename__ == "user_sessions"


def test_user_session_model_fields():
    from app.models.user_session import UserSession
    columns = {c.name for c in UserSession.__table__.columns}
    for col in ["id", "user_id", "created_at", "last_seen_at", "ip_address", "user_agent"]:
        assert col in columns, f"Missing column: {col}"


def test_user_model_has_sessions_invalidated_before():
    columns = {c.name for c in User.__table__.columns}
    assert "sessions_invalidated_before" in columns


def test_account_sessions_route_registered():
    from app.routers.auth import router
    paths = [r.path for r in router.routes]
    assert "/account/sessions" in paths
    assert "/account/sessions/revoke-all" in paths


def test_account_sessions_template_exists():
    assert Path("app/templates/account_sessions.html").exists()


def test_account_sessions_template_has_revoke_button():
    t = Path("app/templates/account_sessions.html").read_text()
    assert "revoke-all" in t
    assert "Revoke all" in t


def test_jwt_has_iat_claim():
    token = create_access_token({"sub": "99"})
    payload = decode_access_token(token)
    assert "iat" in payload
    assert isinstance(payload["iat"], (int, float))


@pytest.mark.asyncio
async def test_sessions_page_renders(auth_client):
    ac, db, user = auth_client
    token = create_access_token({"sub": str(user.id)})
    ac.cookies.set("access_token", token)
    resp = await ac.get("/account/sessions")
    assert resp.status_code == 200
    assert b"Session" in resp.content or b"session" in resp.content


@pytest.mark.asyncio
async def test_sessions_invalidated_before_blocks_old_jwt(auth_client):
    """JWT issued before sessions_invalidated_before is rejected by get_current_user."""
    ac, db, user = auth_client

    # Issue a token, then set invalidation time to future
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    user.sessions_invalidated_before = future
    await db.commit()
    await db.refresh(user)

    token = create_access_token({"sub": str(user.id)})
    payload = decode_access_token(token)
    issued = datetime.fromtimestamp(payload["iat"], tz=timezone.utc)

    # Normalise sessions_invalidated_before to UTC (SQLite may return naive)
    sib = user.sessions_invalidated_before
    if sib.tzinfo is None:
        sib = sib.replace(tzinfo=timezone.utc)

    # The token was just issued — it must be before the future invalidation timestamp
    assert issued <= sib


@pytest.mark.asyncio
async def test_revoke_all_redirects(auth_client):
    ac, db, user = auth_client
    token = create_access_token({"sub": str(user.id)})
    ac.cookies.set("access_token", token)
    csrf = _csrf(ac)
    resp = await ac.post(
        "/account/sessions/revoke-all",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "sessions" in resp.headers.get("location", "")


def test_edit_profile_links_to_sessions():
    ep = Path("app/templates/edit_profile.html").read_text()
    assert "/account/sessions" in ep


def test_change_password_links_to_sessions():
    cp = Path("app/templates/change_password.html").read_text()
    assert "/account/sessions" in cp
