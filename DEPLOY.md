# Deployment Guide

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set SECRET_KEY, SMTP_*, APP_BASE_URL

PYTHONPATH=. alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Visit http://localhost:8000 and register an account. The first admin must be activated directly in the DB:

```bash
sqlite3 ibf_app.db "UPDATE users SET is_active=1, role='admin' WHERE email='you@example.com';"
```

## Running tests

```bash
pip install -r requirements.txt
pytest
```

## Docker

Build and run with the provided `docker-compose.yml`. The app uses SQLite by default; for production switch to PostgreSQL by updating `DATABASE_URL` in `.env`.

```bash
docker build -t ibf-app .
docker run -p 8000:8000 --env-file .env ibf-app
```

Or with compose (adds a Postgres container):

```bash
docker-compose up -d
```

Create a minimal `Dockerfile` if not present:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONPATH=/app
RUN alembic upgrade head
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## systemd service (Linux)

Create `/etc/systemd/system/ibf-app.service`:

```ini
[Unit]
Description=IBF App
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/ibf_app
EnvironmentFile=/opt/ibf_app/.env
ExecStart=/opt/ibf_app/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ibf-app
```

## nginx reverse proxy

```nginx
server {
    listen 80;
    server_name yourdomain.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 50M;
    }
}
```

Use Certbot for HTTPS: `sudo certbot --nginx -d yourdomain.example.com`

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✓ | SQLAlchemy URL, e.g. `sqlite+aiosqlite:///./ibf_app.db` |
| `SECRET_KEY` | ✓ | At least 32 random chars — use `python -c "import secrets; print(secrets.token_hex(32))"` |
| `APP_BASE_URL` | ✓ | Public URL used in emails, e.g. `https://ibf.example.com` |
| `SMTP_HOST` | — | Leave blank to disable email |
| `SMTP_PORT` | — | Default 587 |
| `SMTP_USER` | — | SMTP username |
| `SMTP_PASSWORD` | — | SMTP password |
| `SMTP_FROM` | — | From address for outgoing mail |
| `SMTP_FAILURE_ALERT_AFTER` | — | Consecutive sync failures before admin alert (default 3) |
| `LOG_FILE` | — | Path to rotating log file (default `ibf_app.log`) |
| `LOG_LEVEL` | — | Python log level (default `INFO`) |

## Database migrations

```bash
# Apply all pending migrations
PYTHONPATH=. alembic upgrade head

# Create a new migration after model changes
PYTHONPATH=. alembic revision --autogenerate -m "describe change"
```
