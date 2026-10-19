#!/usr/bin/env bash
# One-time VPS setup for verquoai.com (Ubuntu 24.04). Run as root:
#   bash setup.sh <git-repo-url> <email-for-letsencrypt>
# Re-running is safe; it pulls the latest code and restarts the service.
set -euo pipefail

REPO_URL="${1:?usage: setup.sh <git-repo-url> <email>}"
LE_EMAIL="${2:?usage: setup.sh <git-repo-url> <email>}"
DOMAIN=verquoai.com
BASE=/opt/verquoai
APP="$BASE/app"

apt-get update
apt-get -y upgrade
apt-get install -y python3 python3-venv python3-pip git nginx certbot python3-certbot-nginx ufw

id verquo >/dev/null 2>&1 || useradd --system --home "$BASE" --shell /usr/sbin/nologin verquo
mkdir -p "$BASE"

if [ -d "$APP/.git" ]; then
    git -C "$APP" pull --ff-only
else
    git clone "$REPO_URL" "$APP"
fi
mkdir -p "$APP/logs" "$APP/data/historical"

[ -d "$BASE/venv" ] || python3 -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install --upgrade pip
"$BASE/venv/bin/pip" install -r "$APP/requirements.txt"

if [ ! -f "$APP/.env" ]; then
    cp "$APP/.env.template" "$APP/.env"
    sed -i "s/^SESSION_SECRET=.*/SESSION_SECRET=$(openssl rand -hex 32)/" "$APP/.env"
    echo ">>> Created $APP/.env from template - edit it with your Kite and SMTP values."
fi
chmod 600 "$APP/.env"
chown -R verquo:verquo "$BASE"

cp "$APP/deploy/verquoai.service" /etc/systemd/system/verquoai.service
cp "$APP/deploy/nginx-verquoai.conf" /etc/nginx/sites-available/verquoai
ln -sf /etc/nginx/sites-available/verquoai /etc/nginx/sites-enabled/verquoai
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl daemon-reload
systemctl enable --now verquoai
systemctl restart verquoai
systemctl reload nginx

ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

certbot --nginx --non-interactive --agree-tos --redirect -m "$LE_EMAIL" -d "$DOMAIN" -d "www.$DOMAIN"

echo ">>> Done. Check: systemctl status verquoai ; journalctl -u verquoai -f"
