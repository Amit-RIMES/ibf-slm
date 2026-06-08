import os
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# Use in-memory SQLite for tests
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-tests-only-32x")
os.environ.setdefault("LOG_FILE", "/dev/null")


from app.core.database import Base, get_db  # noqa: E402 — env must be set first
from app.main import app  # noqa: E402

_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
_SessionLocal = sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def _override_get_db():
    async with _SessionLocal() as session:
        yield session


app.dependency_overrides[get_db] = _override_get_db


@pytest_asyncio.fixture(autouse=True, scope="function")
async def reset_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def db():
    async with _SessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def admin_user(db: AsyncSession):
    from app.core.security import hash_password
    from app.models.user import User
    user = User(
        email="admin@test.com",
        username="admin",
        hashed_password=hash_password("Admin1234"),
        is_active=True,
        role="admin",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture
async def api_key(db: AsyncSession, admin_user):
    import hashlib, secrets
    from app.models.api_key import APIKey
    raw = secrets.token_hex(32)
    key = APIKey(
        name="test-key",
        key_prefix=raw[:12],
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        user_id=admin_user.id,
    )
    db.add(key)
    await db.commit()
    return raw


async def _login(client, email="admin@test.com", password="Admin1234"):
    resp = await client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    return resp
