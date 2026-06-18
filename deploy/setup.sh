#!/usr/bin/env bash
# Production setup for IBF App on a RIMES server running Ubuntu 22.04+.
# Run as root (or sudo) once on a fresh server.
set -euo pipefail

APP_DIR=/opt/ibf_app
APP_USER=ibfapp
PYTHON=${PYTHON:-python3.11}

echo "==> Creating application user"
id "$APP_USER" &>/dev/null || useradd --system --shell /usr/sbin/nologin --home "$APP_DIR" "$APP_USER"

echo "==> Installing system packages"
apt-get update -q
apt-get install -y -q python3.11 python3.11-venv python3-pip nginx sqlite3 curl

echo "==> Installing Ollama"
if ! command -v ollama &>/dev/null; then
  curl -fsSL https://ollama.ai/install.sh | sh
fi
systemctl enable --now ollama
sleep 3
ollama pull gemma4:e4b

echo "==> Cloning / updating app"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone https://github.com/Amit-RIMES/ibf-slm "$APP_DIR"
fi

echo "==> Setting up Python environment"
"$PYTHON" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Creating .env from template"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/deploy/.env.production" "$APP_DIR/.env"
  echo "  ** Edit $APP_DIR/.env before starting the service **"
fi

echo "==> Running database migrations"
cd "$APP_DIR"
"$APP_DIR/.venv/bin/alembic" upgrade head

echo "==> Installing systemd service"
cp "$APP_DIR/deploy/ibf_app.service" /etc/systemd/system/ibf_app.service
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
systemctl daemon-reload
systemctl enable ibf_app
systemctl restart ibf_app
systemctl status ibf_app --no-pager

echo "==> Installing nginx config"
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/ibf_app
ln -sf /etc/nginx/sites-available/ibf_app /etc/nginx/sites-enabled/ibf_app
nginx -t
systemctl reload nginx

echo ""
echo "IBF App deployed. Visit https://ibf.rimes.int"
echo "Check logs with: journalctl -u ibf_app -f"
