"""Tests for batch-25 improvements:
  1. AI context / llm.py model comment update
  2. Session expiry UX banner in base.html
  3. Shift handover localStorage persistence
  4. Station data trigger evaluation
  5. Markdown rendering in chat
  6. RIMES deployment artefacts
"""
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import Base, get_db
from app.main import app
from app.models.station import Station, StationObservation
from app.models.trigger import STATION_VARIABLES, VARIABLES, Trigger, TriggerActivation
from app.models.user import User

# ── Test DB ──────────────────────────────────────────────────────────────────

TEST_DB = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine(TEST_DB, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture()
async def auth_client(db_session: AsyncSession):
    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db

    user = User(
        email="test@rimes.int",
        username="tester",
        hashed_password="x",
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    async with AsyncClient(app=app, base_url="http://test") as ac:
        ac.cookies.set("session_token", "fake-token")
        yield ac, db_session, user

    app.dependency_overrides.clear()


# ── 1. AI context — llm.py model comment ─────────────────────────────────────

def test_llm_docstring_references_gemma4():
    from app.core import llm
    import inspect
    doc = inspect.getdoc(llm) or ""
    assert "gemma4:e4b" in doc, "llm.py docstring should mention gemma4:e4b"


def test_llm_num_ctx_comment_updated():
    llm_path = Path("app/core/llm.py")
    source = llm_path.read_text()
    assert "qwen2.5:7b supports up to 32k" not in source, "stale qwen2.5:7b num_ctx comment should be removed"
    assert "gemma4:e4b" in source


# ── 2. Session expiry UX ──────────────────────────────────────────────────────

def test_base_html_session_banner_present():
    base = Path("app/templates/base.html").read_text()
    assert "session-expired-banner" in base
    assert "/login" in base


def test_base_html_fetch_interceptor():
    base = Path("app/templates/base.html").read_text()
    assert "status === 401" in base
    assert "session-expired-banner" in base


def test_base_html_banner_dismiss_button():
    base = Path("app/templates/base.html").read_text()
    assert "Your session has expired" in base


# ── 3. Shift handover localStorage persistence ───────────────────────────────

def test_handover_localStorage_save_function():
    ho = Path("app/templates/handover.html").read_text()
    assert "ibf_handover" in ho
    assert "localStorage.setItem" in ho


def test_handover_localStorage_restore_on_load():
    ho = Path("app/templates/handover.html").read_text()
    assert "localStorage.getItem" in ho
    assert "DOMContentLoaded" in ho


def test_handover_saves_all_fields():
    ho = Path("app/templates/handover.html").read_text()
    for field_id in ["outgoing-name", "incoming-name", "handover-notes"]:
        assert field_id in ho, f"handover.html should reference {field_id}"


# ── 4. Station data trigger evaluation ───────────────────────────────────────

def test_station_variables_in_model():
    assert "station_precip_24h" in STATION_VARIABLES
    assert "station_precip_48h" in STATION_VARIABLES


def test_station_variables_in_global_variables():
    assert "station_precip_24h" in VARIABLES
    assert "station_precip_48h" in VARIABLES


def test_station_trigger_labels_in_router():
    from app.routers.triggers import VARIABLE_LABELS
    assert "station_precip_24h" in VARIABLE_LABELS
    assert "station_precip_48h" in VARIABLE_LABELS


def test_station_triggers_module_importable():
    from app.core.station_triggers import evaluate_station_triggers
    assert callable(evaluate_station_triggers)


@pytest.mark.asyncio
async def test_evaluate_station_triggers_no_triggers(db_session: AsyncSession):
    """Returns 0 when no station triggers are defined."""
    from app.core.station_triggers import evaluate_station_triggers
    result = await evaluate_station_triggers(db_session, date.today())
    assert result == 0


@pytest.mark.asyncio
async def test_evaluate_station_triggers_fires(db_session: AsyncSession):
    """Station trigger fires when max precip exceeds threshold."""
    from app.core.station_triggers import evaluate_station_triggers

    # Create a station trigger
    trigger = Trigger(
        name="Heavy rain station",
        hazard_type="flood",
        variable="station_precip_24h",
        operator="gte",
        threshold=20.0,
        is_active=True,
    )
    db_session.add(trigger)

    # Create a station and observation that exceeds threshold
    station = Station(station_id="ST01", name="Test Station", lat=10.0, lon=100.0, source="test")
    db_session.add(station)
    obs = StationObservation(
        station_id="ST01",
        obs_date=date.today(),
        precip_mm=25.0,
        source="test",
    )
    db_session.add(obs)
    await db_session.commit()

    count = await evaluate_station_triggers(db_session, date.today())
    assert count == 1

    # Activation should be in the DB
    act = await db_session.scalar(
        select(TriggerActivation).where(TriggerActivation.trigger_id == trigger.id)
    )
    assert act is not None
    assert act.value == 25.0
    assert act.status == "active"


@pytest.mark.asyncio
async def test_evaluate_station_triggers_no_fire_below_threshold(db_session: AsyncSession):
    """Station trigger does not fire when precip is below threshold."""
    from app.core.station_triggers import evaluate_station_triggers

    trigger = Trigger(
        name="High threshold trigger",
        hazard_type="flood",
        variable="station_precip_24h",
        operator="gte",
        threshold=100.0,
        is_active=True,
    )
    db_session.add(trigger)
    station = Station(station_id="ST02", name="Station 2", lat=10.0, lon=101.0, source="test")
    db_session.add(station)
    obs = StationObservation(
        station_id="ST02",
        obs_date=date.today(),
        precip_mm=15.0,
        source="test",
    )
    db_session.add(obs)
    await db_session.commit()

    count = await evaluate_station_triggers(db_session, date.today())
    assert count == 0


def test_stations_router_imports_station_triggers():
    source = Path("app/routers/stations.py").read_text()
    assert "evaluate_station_triggers" in source
    assert "from app.core.station_triggers import evaluate_station_triggers" in source


# ── 5. Markdown rendering in chat ────────────────────────────────────────────

def test_chat_loads_marked_js():
    chat = Path("app/templates/chat.html").read_text()
    assert "marked" in chat
    assert "cdn.jsdelivr.net" in chat


def test_chat_uses_marked_parse():
    chat = Path("app/templates/chat.html").read_text()
    assert "marked.parse" in chat


def test_chat_bubble_css_has_markdown_rules():
    chat = Path("app/templates/chat.html").read_text()
    assert ".msg.assistant .bubble p" in chat
    assert ".msg.assistant .bubble ul" in chat
    assert ".msg.assistant .bubble code" in chat


def test_chat_user_bubble_still_preformat():
    """User messages should still be rendered as plain text (white-space: pre-wrap)."""
    chat = Path("app/templates/chat.html").read_text()
    assert "white-space: pre-wrap" in chat


# ── 6. RIMES deployment artefacts ────────────────────────────────────────────

def test_deploy_systemd_service_exists():
    svc = Path("deploy/ibf_app.service")
    assert svc.exists(), "deploy/ibf_app.service must exist"


def test_deploy_service_references_uvicorn():
    svc = Path("deploy/ibf_app.service").read_text()
    assert "uvicorn" in svc
    assert "app.main:app" in svc


def test_deploy_nginx_conf_exists():
    nginx = Path("deploy/nginx.conf")
    assert nginx.exists(), "deploy/nginx.conf must exist"


def test_deploy_nginx_conf_sse_unbuffered():
    nginx = Path("deploy/nginx.conf").read_text()
    assert "proxy_buffering    off" in nginx or "proxy_buffering off" in nginx
    assert "/api/v1/stream" in nginx


def test_deploy_setup_script_exists():
    script = Path("deploy/setup.sh")
    assert script.exists(), "deploy/setup.sh must exist"


def test_deploy_setup_installs_ollama_and_gemma4():
    script = Path("deploy/setup.sh").read_text()
    assert "ollama" in script
    assert "gemma4:e4b" in script


def test_deploy_env_production_template_exists():
    env_tmpl = Path("deploy/.env.production")
    assert env_tmpl.exists(), "deploy/.env.production template must exist"


def test_deploy_env_production_has_required_keys():
    env = Path("deploy/.env.production").read_text()
    for key in ["SECRET_KEY", "DATABASE_URL", "OLLAMA_MODEL", "CSRF_SECRET"]:
        assert key in env, f"deploy/.env.production should include {key}"
