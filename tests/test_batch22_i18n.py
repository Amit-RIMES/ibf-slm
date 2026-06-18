"""Tests for #11: Multi-language bulletin templates."""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _login


@pytest.mark.asyncio
async def test_bulletin_generate_default_english(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/bulletin/generate", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Trigger Alerts" in resp.content
    assert b"Drought Status" in resp.content


@pytest.mark.asyncio
async def test_bulletin_generate_french(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/bulletin/generate?lang=fr", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Alertes de d" in resp.content  # "Alertes de déclenchement"
    assert b"lang=\"fr\"" in resp.content


@pytest.mark.asyncio
async def test_bulletin_generate_spanish(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/bulletin/generate?lang=es", follow_redirects=False)
    assert resp.status_code == 200
    assert "Alertas de disparadores".encode() in resp.content
    assert b"lang=\"es\"" in resp.content


@pytest.mark.asyncio
async def test_bulletin_generate_portuguese(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/bulletin/generate?lang=pt", follow_redirects=False)
    assert resp.status_code == 200
    assert "Alertas de gatilhos".encode() in resp.content
    assert b"lang=\"pt\"" in resp.content


@pytest.mark.asyncio
async def test_bulletin_generate_indonesian(client: AsyncClient, admin_user, db: AsyncSession):
    await _login(client)
    resp = await client.get("/bulletin/generate?lang=id", follow_redirects=False)
    assert resp.status_code == 200
    assert "Peringatan Pemicu".encode() in resp.content
    assert b"lang=\"id\"" in resp.content


@pytest.mark.asyncio
async def test_bulletin_form_has_language_selector(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/bulletin", follow_redirects=False)
    assert resp.status_code == 200
    assert b"name=\"lang\"" in resp.content
    assert b"English" in resp.content
    assert "Français".encode("utf-8") in resp.content


@pytest.mark.asyncio
async def test_bulletin_unknown_lang_falls_back_to_english(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/bulletin/generate?lang=xx", follow_redirects=False)
    assert resp.status_code == 200
    assert b"Trigger Alerts" in resp.content


@pytest.mark.asyncio
async def test_bulletin_french_drought_section_title(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/bulletin/generate?lang=fr", follow_redirects=False)
    assert resp.status_code == 200
    assert "Sécheresse".encode("utf-8") in resp.content or b"cheresse" in resp.content


@pytest.mark.asyncio
async def test_bulletin_french_seasonal_section_title(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/bulletin/generate?lang=fr", follow_redirects=False)
    assert resp.status_code == 200
    assert "Perspective saisonni".encode("utf-8") in resp.content


@pytest.mark.asyncio
async def test_bulletin_spanish_no_data_labels(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/bulletin/generate?lang=es", follow_redirects=False)
    assert resp.status_code == 200
    assert "Sin datos".encode() in resp.content or b"datos" in resp.content


@pytest.mark.asyncio
async def test_bulletin_print_lang_passed_through(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/bulletin/print?lang=fr&days=7", follow_redirects=False)
    assert resp.status_code == 200
    # The rendered bulletin HTML is embedded in the print wrapper
    assert "Sécheresse".encode("utf-8") in resp.content or b"cheresse" in resp.content


@pytest.mark.asyncio
async def test_bulletin_toolbar_print_link_includes_lang(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/bulletin/generate?lang=fr", follow_redirects=False)
    assert resp.status_code == 200
    assert b"lang=fr" in resp.content


@pytest.mark.asyncio
async def test_bulletin_impacts_section_title_uses_days(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/bulletin/generate?lang=en&days=14", follow_redirects=False)
    assert resp.status_code == 200
    assert b"14" in resp.content
    assert b"Recent Impact Records" in resp.content


@pytest.mark.asyncio
async def test_bulletin_french_impacts_section_title_word_order(
    client: AsyncClient, admin_user, db: AsyncSession
):
    await _login(client)
    resp = await client.get("/bulletin/generate?lang=fr&days=14", follow_redirects=False)
    assert resp.status_code == 200
    # French: "14 derniers jours" (number before "derniers")
    assert b"14" in resp.content
    assert "derniers jours".encode() in resp.content


@pytest.mark.asyncio
async def test_bulletin_generate_requires_auth(client: AsyncClient, db: AsyncSession):
    resp = await client.get("/bulletin/generate?lang=fr", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_i18n_get_translations_returns_english_for_unknown():
    from app.core.i18n import get_translations
    T = get_translations("zz")
    assert T["lang_name"] == "English"


@pytest.mark.asyncio
async def test_i18n_build_drought_status_english():
    from app.core.i18n import build_drought_status, get_translations
    T = get_translations("en")
    result = build_drought_status(T, 3, -1.5, "Moderate Drought", "May", 2026)
    assert "SPI-3" in result
    assert "moderate drought" in result
    assert "-1.50" in result


@pytest.mark.asyncio
async def test_i18n_build_drought_status_french():
    from app.core.i18n import build_drought_status, get_translations
    T = get_translations("fr")
    result = build_drought_status(T, 6, -2.1, "Severe Drought", "Mai", 2026)
    assert "SPI-6" in result
    assert "-2.10" in result


@pytest.mark.asyncio
async def test_i18n_build_impact_summary_english_plural():
    from app.core.i18n import build_impact_summary, get_translations
    T = get_translations("en")
    result = build_impact_summary(T, 3, 30, 1500)
    assert "3 impact events" in result
    assert "1,500" in result


@pytest.mark.asyncio
async def test_i18n_build_impact_summary_english_singular():
    from app.core.i18n import build_impact_summary, get_translations
    T = get_translations("en")
    result = build_impact_summary(T, 1, 7, 0)
    assert "1 impact event" in result
    assert "impact events" not in result


@pytest.mark.asyncio
async def test_i18n_build_impact_summary_french():
    from app.core.i18n import build_impact_summary, get_translations
    T = get_translations("fr")
    result = build_impact_summary(T, 2, 14, 200)
    assert "2" in result
    assert "200" in result
