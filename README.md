# IBF App

A prototype application for impact-based forecasting data processing, reasoning-ready corpus development, and model evaluation.

## Stack

- **FastAPI** — async web framework
- **SQLAlchemy 2 + asyncpg** — async ORM with PostgreSQL
- **Alembic** — database migrations
- **bcrypt + python-jose** — password hashing and JWT tokens
- **Jinja2** — server-rendered HTML templates
- **Docker Compose** — local PostgreSQL

## Project structure

```
ibf_app/
├── app/
│   ├── core/
│   │   ├── config.py        # Settings from .env
│   │   ├── database.py      # Async engine + session
│   │   └── security.py      # bcrypt hashing, JWT helpers
│   ├── models/
│   │   └── user.py          # User model
│   ├── routers/
│   │   ├── auth.py          # /register  /login  /logout
│   │   └── dashboard.py     # /dashboard (protected)
│   ├── templates/           # Jinja2 HTML templates
│   └── main.py              # App entry point
├── alembic/                 # Migration scripts
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Getting started

**1. Start PostgreSQL**

```bash
docker-compose up -d
```

**2. Configure environment**

```bash
cp .env.example .env
```

Edit `.env` and set a strong `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**3. Install dependencies**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**4. Run**

```bash
uvicorn app.main:app --reload
```

Tables are created automatically on first startup. No migration step needed for a fresh install.

## Routes

| Route | Description |
|---|---|
| `GET /` | Redirects to `/login` |
| `GET /register` | Registration page |
| `POST /register` | Create account |
| `GET /login` | Login page |
| `POST /login` | Authenticate, set session cookie |
| `GET /dashboard` | Protected dashboard |
| `GET /logout` | Clear session, redirect to login |

## Auth flow

Authentication uses **httponly JWT cookies** — no JavaScript required. On login the server sets an `access_token` cookie; the browser sends it automatically on every subsequent request. Visiting `/dashboard` without a valid token redirects to `/login`.

## Database migrations

Alembic is wired up for production use. After changing a model:

```bash
alembic revision --autogenerate -m "describe your change"
alembic upgrade head
```
