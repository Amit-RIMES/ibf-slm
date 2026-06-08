import pytest
from httpx import AsyncClient


async def test_register_and_login(client: AsyncClient, admin_user):
    # Registration creates an inactive user (needs admin approval)
    resp = await client.post("/register", data={
        "username": "newuser", "email": "new@test.com", "password": "NewUser1"
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "pending=1" in resp.headers["location"]


async def test_register_password_too_short(client: AsyncClient):
    resp = await client.post("/register", data={
        "username": "u", "email": "u@test.com", "password": "ab1"
    })
    assert resp.status_code == 400
    assert "8 characters" in resp.text


async def test_register_password_no_digit(client: AsyncClient):
    resp = await client.post("/register", data={
        "username": "u", "email": "u@test.com", "password": "abcdefgh"
    })
    assert resp.status_code == 400
    assert "digit" in resp.text


async def test_register_password_no_letter(client: AsyncClient):
    resp = await client.post("/register", data={
        "username": "u", "email": "u@test.com", "password": "12345678"
    })
    assert resp.status_code == 400
    assert "letter" in resp.text


async def test_login_success(client: AsyncClient, admin_user):
    resp = await client.post("/login", data={
        "email": "admin@test.com", "password": "Admin1234"
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert "access_token" in resp.cookies


async def test_login_wrong_password(client: AsyncClient, admin_user):
    resp = await client.post("/login", data={
        "email": "admin@test.com", "password": "wrongpass"
    })
    assert resp.status_code == 401
    assert "Invalid" in resp.text


async def test_login_inactive_user(client: AsyncClient, db):
    from app.core.security import hash_password
    from app.models.user import User
    user = User(email="inactive@test.com", username="inactive",
                hashed_password=hash_password("Inactive1"), is_active=False)
    db.add(user)
    await db.commit()

    resp = await client.post("/login", data={"email": "inactive@test.com", "password": "Inactive1"})
    assert resp.status_code == 403
    assert "pending" in resp.text.lower()


async def test_logout(client: AsyncClient, admin_user):
    await client.post("/login", data={"email": "admin@test.com", "password": "Admin1234"})
    resp = await client.get("/logout", follow_redirects=False)
    assert resp.status_code == 303
    # Cookie should be cleared
    assert "access_token" not in resp.cookies or resp.cookies["access_token"] == ""


async def test_duplicate_email_registration(client: AsyncClient, admin_user):
    resp = await client.post("/register", data={
        "username": "other", "email": "admin@test.com", "password": "Other1234"
    })
    assert resp.status_code == 400
    assert "already registered" in resp.text
