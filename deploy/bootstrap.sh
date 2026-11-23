#!/usr/bin/env bash
# One-shot server setup / update for pm-framework. Runs ON THE SERVER as root;
# deploy/push.sh copies the code up and calls this for you.
#
#   DOMAIN=pm.example.com bash /opt/pm-framework/deploy/bootstrap.sh
#
# DOMAIN is optional: without one the server uses <its-ip>.sslip.io, a free
# hostname that resolves to the server's IP (Caddy still gets a real HTTPS
# certificate for it). The chosen domain is remembered in .env (PM_DOMAIN).
#
# Idempotent: re-run after every code update. SKIP_SYSTEMD=1 skips everything
# that needs a real init system (firewall, services, Caddy reload, Twilio,
# health checks) — used to test this script in a container.
set -euo pipefail

APP=/opt/pm-framework
APP_USER=pm
PYTHON_VERSION=3.14
SKIP_SYSTEMD=${SKIP_SYSTEMD:-0}
cd "$APP"

log() { printf '\n==> %s\n' "$*"; }
as_app() { runuser -u "$APP_USER" -- env HOME="/home/$APP_USER" PATH="/usr/local/bin:/usr/bin:/bin" "$@"; }
get_env() { grep -E "^$1=" "$APP/.env" | tail -1 | cut -d= -f2- || true; }
set_env() {
  python3 - "$APP/.env" "$1" "$2" <<'PY'
import sys
path, key, val = sys.argv[1:]
lines = open(path).read().splitlines()
out, done = [], False
for line in lines:
    if line.startswith(key + "="):
        out.append(f"{key}={val}")
        done = True
    else:
        out.append(line)
if not done:
    out.append(f"{key}={val}")
open(path, "w").write("\n".join(out) + "\n")
PY
}

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

log "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq caddy curl ca-certificates python3 openssl ufw >/dev/null
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi

log "App user + permissions"
id -u "$APP_USER" >/dev/null 2>&1 || \
  useradd --system --create-home --home-dir "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "WARNING: no .env was uploaded — created one from .env.example; fill in your keys."
fi
chown -R "$APP_USER:$APP_USER" "$APP"
chmod 755 "$APP" "$APP/dashboard"
chmod 600 .env
find "$APP" -maxdepth 1 \( -name '*.pem' -o -name '*.key' -o -name 'kalshi_private*.txt' \) -exec chmod 600 {} +

log "Domain"
DOMAIN=${DOMAIN:-$(get_env PM_DOMAIN)}
if [ -z "$DOMAIN" ]; then
  IP=$(curl -fsS -4 https://api.ipify.org || curl -fsS -4 https://ifconfig.me)
  DOMAIN="${IP//./-}.sslip.io"
fi
echo "using https://$DOMAIN"
set_env PM_DOMAIN "$DOMAIN"
set_env WEBHOOK_PUBLIC_URL "https://$DOMAIN/sms-reply"
set_env DASHBOARD_URL "https://$DOMAIN"
NEW_PASSWORD=""
if [ -z "$(get_env DASHBOARD_PASSWORD)" ]; then
  NEW_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)
  set_env DASHBOARD_PASSWORD "$NEW_PASSWORD"
fi
chown "$APP_USER:$APP_USER" .env && chmod 600 .env

log "Python $PYTHON_VERSION environment"
if [ ! -x .venv/bin/python ]; then
  as_app uv venv --python "$PYTHON_VERSION" .venv
fi
# CPU-only torch first, so sentence-transformers doesn't pull the multi-GB CUDA build.
as_app uv pip install --quiet --python .venv/bin/python torch \
  --index-url https://download.pytorch.org/whl/cpu
as_app uv pip install --quiet --python .venv/bin/python -r requirements.txt
# Pre-fetch the embedding model so build_candidates works offline later.
as_app .venv/bin/python -c "from sentence_transformers import SentenceTransformer as S; S('all-MiniLM-L6-v2')" >/dev/null 2>&1

log "Database + tests"
as_app .venv/bin/python db/init.py
as_app .venv/bin/python -m pytest -q tests 2>&1 | tail -1

log "Caddy (HTTPS reverse proxy)"
sed "s/your-domain.example/$DOMAIN/" deploy/Caddyfile > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 \
  && echo "Caddyfile valid" || { echo "Caddyfile INVALID"; caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; exit 1; }

if [ "$SKIP_SYSTEMD" = 1 ]; then
  log "SKIP_SYSTEMD=1 — stopping before firewall/services"
  exit 0
fi

systemctl enable --now caddy >/dev/null
systemctl reload caddy

log "Firewall (SSH, HTTP, HTTPS only)"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ufw status | head -8

log "Services"
bash deploy/install.sh

log "Twilio webhook"
as_app .venv/bin/python deploy/twilio_webhook.py || echo "(set it by hand in the Twilio console)"

log "Health checks"
for i in $(seq 1 20); do curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:8000/health >/dev/null && echo "webhook: up" || echo "webhook: DOWN (journalctl -u pm-webhook)"
for i in $(seq 1 30); do curl -fsS -o /dev/null "https://$DOMAIN/" 2>/dev/null && break; sleep 2; done
curl -fsS -o /dev/null "https://$DOMAIN/" && echo "dashboard: https://$DOMAIN (HTTPS ok)" \
  || echo "dashboard: not reachable over HTTPS yet (journalctl -u caddy; DNS may still be propagating)"

log "Done"
echo "Dashboard:   https://$DOMAIN"
echo "SMS webhook: https://$DOMAIN/sms-reply"
if [ -n "$NEW_PASSWORD" ]; then
  echo "Trades-page password (generated, saved in /opt/pm-framework/.env): $NEW_PASSWORD"
fi
