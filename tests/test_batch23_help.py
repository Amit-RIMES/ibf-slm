"""Tests for #12: Onboarding/help docs page."""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


@pytest.mark.asyncio
async def test_help_requires_auth(client: AsyncClient, db: AsyncSession):
    resp = await client.get("/help", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_help_loads_for_user(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/help", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Help" in resp.content


@pytest.mark.asyncio
async def test_help_has_trigger_section(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/help", follow_redirects=False)
    assert resp.status_code == 200
    assert b"What is a Trigger" in resp.content


@pytest.mark.asyncio
async def test_help_has_spi_section(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/help", follow_redirects=False)
    assert resp.status_code == 200
    assert b"SPI" in resp.content
    assert b"Standardised Precipitation Index" in resp.content


@pytest.mark.asyncio
async def test_help_has_wmo_section(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/help", follow_redirects=False)
    assert resp.status_code == 200
    assert b"WMO" in resp.content
    assert b"Warning Levels" in resp.content or b"warning" in resp.content.lower()


@pytest.mark.asyncio
async def test_help_has_bulletin_section(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/help", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Bulletin" in resp.content
    assert b"Approval" in resp.content or b"approval" in resp.content.lower()


@pytest.mark.asyncio
async def test_help_has_alerts_section(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/help", follow_redirects=False)
    assert resp.status_code == 200
    assert b"SMS" in resp.content or b"WhatsApp" in resp.content


@pytest.mark.asyncio
async def test_help_has_verification_section(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/help", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Verification" in resp.content or b"verification" in resp.content.lower()
    assert b"Hit Rate" in resp.content or b"hit rate" in resp.content.lower()


@pytest.mark.asyncio
async def test_help_has_handover_section(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/help", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Handover" in resp.content or b"handover" in resp.content.lower()


@pytest.mark.asyncio
async def test_help_has_data_sources_section(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/help", follow_redirects=False)
    assert resp.status_code == 200
    assert b"CHIRPS" in resp.content
    assert b"ECMWF" in resp.content


@pytest.mark.asyncio
async def test_help_has_toc_links(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/help", follow_redirects=False)
    assert resp.status_code == 200
    assert b"#what-is-a-trigger" in resp.content
    assert b"#spi" in resp.content
    assert b"#wmo-levels" in resp.content


@pytest.mark.asyncio
async def test_help_has_nav_link(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/help", follow_redirects=False)
    assert resp.status_code == 200
    assert b'href="/help"' in resp.content


@pytest.mark.asyncio
async def test_dashboard_has_help_nav_link(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 200
    assert b'href="/help"' in resp.content
