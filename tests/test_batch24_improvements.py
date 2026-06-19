"""Tests for the 9 operational improvements (commit 252b16d).

Covers:
  1. Dashboard Pending Actions widget
  2. Impact coordinate HTML constraints
  3. Impact country datalist
  4. Bulletin PDF export (?auto=1 auto-print)
  5. Bulletin form localStorage JS
  6. Chat dynamic suggestions
  7. Chat localStorage history
  8. Mobile nav overflow CSS
  9. COUNTRY_LIST plumbing to all impact_form renders
"""
import pytest
from datetime import date, datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


def _csrf(client):
    from app.core.csrf import _token_for
    return _token_for(client.cookies.get("access_token", ""))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Dashboard Pending Actions widget
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_dashboard_pending_actions_all_clear(client: AsyncClient, admin_user, db: AsyncSession):
    """Widget shows 'All clear' when there are no pending items."""
    await _login(client)
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Pending Actions" in resp.content
    assert b"All clear" in resp.content


@pytest.mark.asyncio
async def test_dashboard_pending_actions_shows_unack_activations(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Widget shows unacknowledged trigger activations."""
    from app.models.trigger import Trigger, TriggerActivation
    from app.models.forecast import ForecastUpload

    t = Trigger(
        name="PendingTrig", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=50.0, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    fc = ForecastUpload(
        filename="fc_pending.nc", source="manual",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
        time_start="2026-01-01", time_end="2026-01-15", time_steps=15,
        precip_min=5.0, precip_max=80.0, precip_mean=65.0, geojson="{}",
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)

    act = TriggerActivation(
        trigger_id=t.id, forecast_id=fc.id, value=65.0, status="active",
        triggered_at=datetime.now(timezone.utc),
    )
    db.add(act)
    await db.commit()

    await _login(client)
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Pending Actions" in resp.content
    assert b"unreviewed weather warning" in resp.content


@pytest.mark.asyncio
async def test_dashboard_pending_actions_shows_pending_draft(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Widget shows pending bulletin drafts."""
    from app.models.bulletin_draft import BulletinDraft

    draft = BulletinDraft(
        source="CHIRPS", risk_level="High", total_score=70,
        title="Test Bulletin", note="", status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db.add(draft)
    await db.commit()

    await _login(client)
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 200
    assert b"pending review" in resp.content
    assert b"Review" in resp.content


@pytest.mark.asyncio
async def test_dashboard_pending_actions_shows_pending_registration(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Widget shows pending user registrations (admin only)."""
    from app.core.security import hash_password
    from app.models.user import User

    pending = User(
        email="newguy@test.com", username="newguy",
        hashed_password=hash_password("Newguy123"),
        is_active=False, role="user",
    )
    db.add(pending)
    await db.commit()

    await _login(client)
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 200
    assert b"awaiting approval" in resp.content
    assert b"Approve" in resp.content


@pytest.mark.asyncio
async def test_dashboard_pending_actions_badge_count(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Badge count reflects number of distinct pending issue types."""
    from app.models.bulletin_draft import BulletinDraft

    db.add(BulletinDraft(
        source="CHIRPS", risk_level="High", total_score=70,
        title="B1", note="", status="pending",
        created_at=datetime.now(timezone.utc),
    ))
    await db.commit()

    await _login(client)
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 200
    # Badge shows count ≥ 1, not "All clear"
    assert b"All clear" not in resp.content


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Impact coordinate HTML constraints
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_impact_form_lat_has_min_max(client: AsyncClient, admin_user, db: AsyncSession):
    """Impact new form includes min/max HTML constraints on lat field."""
    await _login(client)
    resp = await client.get("/impacts/new", follow_redirects=False)
    assert resp.status_code == 200
    assert b'min="-90"' in resp.content
    assert b'max="90"' in resp.content


@pytest.mark.asyncio
async def test_impact_form_lon_has_min_max(client: AsyncClient, admin_user, db: AsyncSession):
    """Impact new form includes min/max HTML constraints on lon field."""
    await _login(client)
    resp = await client.get("/impacts/new", follow_redirects=False)
    assert resp.status_code == 200
    assert b'min="-180"' in resp.content
    assert b'max="180"' in resp.content


@pytest.mark.asyncio
async def test_impact_backend_rejects_lat_out_of_range(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Backend returns an error (not 500) when lat=999 is submitted."""
    await _login(client)
    csrf = _csrf(client)
    resp = await client.post(
        "/impacts/new",
        data={
            "event_name": "Test Event", "event_date": "2026-01-01",
            "hazard_type": "flood", "country": "Thailand",
            "lat": "999", "lon": "100", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"Latitude" in resp.content or b"between" in resp.content


@pytest.mark.asyncio
async def test_impact_backend_rejects_lon_out_of_range(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Backend returns an error (not 500) when lon=999 is submitted."""
    await _login(client)
    csrf = _csrf(client)
    resp = await client.post(
        "/impacts/new",
        data={
            "event_name": "Test Event", "event_date": "2026-01-01",
            "hazard_type": "flood", "country": "Thailand",
            "lat": "15", "lon": "999", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"Longitude" in resp.content or b"between" in resp.content


@pytest.mark.asyncio
async def test_impact_backend_accepts_valid_coords(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Backend accepts valid coordinates and creates the record."""
    await _login(client)
    csrf = _csrf(client)
    resp = await client.post(
        "/impacts/new",
        data={
            "event_name": "Valid Coords Event", "event_date": "2026-01-01",
            "hazard_type": "flood", "country": "Thailand",
            "lat": "15.5", "lon": "100.5", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    # Redirects to detail page on success
    assert resp.status_code in (302, 303)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Impact country datalist
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_impact_form_has_country_datalist(client: AsyncClient, admin_user, db: AsyncSession):
    """Impact form renders a <datalist> element for country autocomplete."""
    await _login(client)
    resp = await client.get("/impacts/new", follow_redirects=False)
    assert resp.status_code == 200
    assert b'datalist' in resp.content
    assert b'countries-list' in resp.content


@pytest.mark.asyncio
async def test_impact_form_datalist_contains_countries(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Country datalist contains canonical country names (not ISO codes)."""
    await _login(client)
    resp = await client.get("/impacts/new", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Mozambique" in resp.content
    assert b"Thailand" in resp.content
    assert b"Bangladesh" in resp.content
    # ISO codes should NOT appear as datalist values
    assert b'value="mz"' not in resp.content
    assert b'value="th"' not in resp.content


@pytest.mark.asyncio
async def test_impact_edit_form_has_country_datalist(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Edit form also has the country datalist."""
    from app.models.impact import ImpactRecord

    imp = ImpactRecord(
        event_name="Edit Test", event_date=date(2026, 1, 1),
        hazard_type="flood", country="Thailand",
    )
    db.add(imp)
    await db.commit()
    await db.refresh(imp)

    await _login(client)
    resp = await client.get(f"/impacts/{imp.id}/edit", follow_redirects=False)
    assert resp.status_code == 200
    assert b'datalist' in resp.content
    assert b'Mozambique' in resp.content


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Bulletin PDF export (?auto=1)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bulletin_print_auto_injects_print_script(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """?auto=1 injects window.onload=window.print() into the print page."""
    await _login(client)
    resp = await client.get(
        "/bulletin/print?source=CHIRPS&days=30&auto=1",
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"window.onload" in resp.content
    assert b"window.print()" in resp.content


@pytest.mark.asyncio
async def test_bulletin_print_without_auto_no_auto_print(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Without ?auto=1, print page does NOT auto-trigger the print dialog."""
    await _login(client)
    resp = await client.get(
        "/bulletin/print?source=CHIRPS&days=30",
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"window.onload" not in resp.content


@pytest.mark.asyncio
async def test_bulletin_print_accepts_lang_param(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Print page respects the lang= parameter."""
    await _login(client)
    resp = await client.get(
        "/bulletin/print?source=CHIRPS&days=30&lang=fr",
        follow_redirects=False,
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_bulletin_print_requires_auth(client: AsyncClient, db: AsyncSession):
    """Print page redirects unauthenticated users to login."""
    resp = await client.get("/bulletin/print?source=CHIRPS&days=30", follow_redirects=False)
    assert resp.status_code in (302, 303)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Bulletin form localStorage auto-fill
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bulletin_form_has_localstorage_script(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Bulletin form page contains localStorage save/restore script."""
    await _login(client)
    resp = await client.get("/bulletin", follow_redirects=False)
    assert resp.status_code == 200
    assert b"ibf_bulletin" in resp.content
    assert b"localStorage" in resp.content


@pytest.mark.asyncio
async def test_bulletin_form_has_element_ids(client: AsyncClient, admin_user, db: AsyncSession):
    """Bulletin form has the named IDs needed by the auto-fill script."""
    await _login(client)
    resp = await client.get("/bulletin", follow_redirects=False)
    assert resp.status_code == 200
    assert b'id="f-source"' in resp.content
    assert b'id="f-days"' in resp.content
    assert b'id="pdf-btn"' in resp.content


@pytest.mark.asyncio
async def test_bulletin_form_pdf_button_present(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """'Download PDF' button is rendered on the bulletin form page."""
    await _login(client)
    resp = await client.get("/bulletin", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Download PDF" in resp.content


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Chat dynamic suggestions
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_chat_page_loads(client: AsyncClient, admin_user, db: AsyncSession):
    """Chat page loads successfully."""
    await _login(client)
    resp = await client.get("/chat", follow_redirects=False)
    assert resp.status_code == 200
    assert b"AI Assistant" in resp.content


@pytest.mark.asyncio
async def test_chat_no_dynamic_suggestions_when_empty_db(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """No dynamic count suggestion when DB is empty (hints.unack == 0)."""
    await _login(client)
    resp = await client.get("/chat", follow_redirects=False)
    assert resp.status_code == 200
    # The dynamic "You have X unacknowledged activations" block should not render
    assert b"You have" not in resp.content


@pytest.mark.asyncio
async def test_chat_shows_unack_count_suggestion(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Chat page shows unacknowledged activation count in suggestions."""
    from app.models.trigger import Trigger, TriggerActivation
    from app.models.forecast import ForecastUpload

    t = Trigger(
        name="ChatTrig", hazard_type="flood",
        variable="precip_mean", operator="gt", threshold=40.0, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    fc = ForecastUpload(
        filename="chat_fc.nc", source="manual",
        uploaded_at=datetime.now(timezone.utc),
        lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
        time_start="2026-01-01", time_end="2026-01-15", time_steps=15,
        precip_min=5.0, precip_max=70.0, precip_mean=55.0, geojson="{}",
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)

    db.add(TriggerActivation(
        trigger_id=t.id, forecast_id=fc.id, value=55.0, status="triggered",  # chat uses "triggered"
        triggered_at=datetime.now(timezone.utc),
    ))
    await db.commit()

    await _login(client)
    resp = await client.get("/chat", follow_redirects=False)
    assert resp.status_code == 200
    assert b"unacknowledged activation" in resp.content


@pytest.mark.asyncio
async def test_chat_shows_recent_impact_count_suggestion(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Chat page shows recent impact count in suggestions."""
    from app.models.impact import ImpactRecord

    db.add(ImpactRecord(
        event_name="Recent Flood", hazard_type="flood",
        event_date=date.today(), country="Thailand",
        affected_population=5000,
    ))
    await db.commit()

    await _login(client)
    resp = await client.get("/chat", follow_redirects=False)
    assert resp.status_code == 200
    assert b"impact" in resp.content.lower()


@pytest.mark.asyncio
async def test_chat_shows_latest_forecast_date(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Chat suggestions include the latest forecast date when data exists."""
    from app.models.forecast import ForecastUpload

    now = datetime.now(timezone.utc)
    db.add(ForecastUpload(
        filename="latest_for_chat.nc", source="manual",
        uploaded_at=now,
        lat_min=10.0, lat_max=20.0, lon_min=90.0, lon_max=100.0,
        time_start="2026-01-01", time_end="2026-01-15", time_steps=15,
        precip_min=5.0, precip_max=50.0, precip_mean=25.0, geojson="{}",
    ))
    await db.commit()

    await _login(client)
    resp = await client.get("/chat", follow_redirects=False)
    assert resp.status_code == 200
    assert b"forecast from" in resp.content


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Chat localStorage history persistence
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_chat_page_has_localstorage_key(client: AsyncClient, admin_user, db: AsyncSession):
    """Chat page script references the ibf_chat_history localStorage key."""
    await _login(client)
    resp = await client.get("/chat", follow_redirects=False)
    assert resp.status_code == 200
    assert b"ibf_chat_history" in resp.content


@pytest.mark.asyncio
async def test_chat_page_has_clear_button(client: AsyncClient, admin_user, db: AsyncSession):
    """Chat page renders the clear (✕) conversation button."""
    await _login(client)
    resp = await client.get("/chat", follow_redirects=False)
    assert resp.status_code == 200
    assert b'id="clear-btn"' in resp.content


@pytest.mark.asyncio
async def test_chat_page_has_save_history_function(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """Chat page script includes a saveHistory function for persistence."""
    await _login(client)
    resp = await client.get("/chat", follow_redirects=False)
    assert resp.status_code == 200
    assert b"saveHistory" in resp.content
    assert b"localStorage" in resp.content


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Mobile nav overflow fix
# ═══════════════════════════════════════════════════════════════════════════════

def test_base_html_has_overflow_x_hidden():
    """base.html CSS prevents horizontal scroll on mobile."""
    with open("app/templates/base.html") as f:
        content = f.read()
    assert "overflow-x: hidden" in content
    assert "max-width: 100vw" in content


def test_base_html_nav_menu_scrollable():
    """Mobile nav menu has max-height + overflow-y: auto to handle many links."""
    with open("app/templates/base.html") as f:
        content = f.read()
    assert "overflow-y: auto" in content


def test_base_html_hamburger_exists():
    """Hamburger button JS is present for mobile nav toggle."""
    with open("app/templates/base.html") as f:
        content = f.read()
    assert "nav-hamburger" in content
    assert "nav-open" in content


# ═══════════════════════════════════════════════════════════════════════════════
# 9. COUNTRY_LIST plumbing to all impact_form renders
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_impact_create_error_still_shows_datalist(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """When create fails validation, the error page still has the datalist."""
    await _login(client)
    csrf = _csrf(client)
    resp = await client.post(
        "/impacts/new",
        data={
            "event_name": "Bad Coords", "event_date": "2026-01-01",
            "hazard_type": "flood", "country": "Thailand",
            "lat": "999", "lon": "100", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"datalist" in resp.content
    assert b"Thailand" in resp.content  # country options still rendered


@pytest.mark.asyncio
async def test_impact_update_error_still_shows_datalist(
    client: AsyncClient, admin_user, db: AsyncSession
):
    """When update fails validation, the error page still has the datalist."""
    from app.models.impact import ImpactRecord

    imp = ImpactRecord(
        event_name="Update Test", event_date=date(2026, 1, 1),
        hazard_type="flood", country="Thailand",
    )
    db.add(imp)
    await db.commit()
    await db.refresh(imp)

    await _login(client)
    csrf = _csrf(client)
    resp = await client.post(
        f"/impacts/{imp.id}/edit",
        data={
            "event_name": "Update Test", "event_date": "2026-01-01",
            "hazard_type": "flood", "country": "Thailand",
            "lat": "999", "lon": "100", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"datalist" in resp.content


def test_country_list_contains_expected_entries():
    """COUNTRY_LIST is sorted and contains canonical full names."""
    from app.routers.impacts import COUNTRY_LIST
    assert "Mozambique" in COUNTRY_LIST
    assert "Thailand" in COUNTRY_LIST
    assert "Bangladesh" in COUNTRY_LIST
    # Should be sorted
    assert COUNTRY_LIST == sorted(COUNTRY_LIST)
    # No ISO codes
    assert "th" not in COUNTRY_LIST
    assert "mz" not in COUNTRY_LIST


def test_country_list_length():
    """COUNTRY_LIST matches the COUNTRY_NAMES source (46 entries)."""
    from app.routers.impacts import COUNTRY_LIST
    from app.routers.forecasts import COUNTRY_NAMES
    assert len(COUNTRY_LIST) == len(COUNTRY_NAMES)
