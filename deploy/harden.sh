#!/usr/bin/env bash
# Server hardening for verquoai.com (Ubuntu 24.04). Run as root once your SSH
# key login works, keeping your current SSH window open until you have tested
# a fresh login in a second window:
#   sudo bash /opt/verquoai/app/deploy/harden.sh <admin-user>
# Safe to re-run.
set -euo pipefail

ADMIN_USER="${1:?usage: harden.sh <admin-user>}"
BASE=/opt/verquoai
APP="$BASE/app"
KEYS="/home/$ADMIN_USER/.ssh/authorized_keys"

# 1. SSH: keys only, no root login. Refuse without a key, or you would be locked out.
if [ ! -s "$KEYS" ]; then
    echo "No SSH key in $KEYS - install your public key first (see the deploy notes)." >&2
    exit 1
fi
# sshd uses the first value it reads, so this file must sort before 50-cloud-init.conf.
cat > /etc/ssh/sshd_config.d/00-verquoai-hardening.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
MaxAuthTries 3
CONF
sshd -t
systemctl reload ssh

# 2. Ban IPs that keep failing SSH logins; install security updates automatically.
apt-get update
apt-get install -y fail2ban unattended-upgrades sqlite3 rsync
cat > /etc/fail2ban/jail.d/verquoai.local <<'CONF'
[sshd]
enabled = true
maxretry = 5
findtime = 10m
bantime = 1h
CONF
systemctl enable fail2ban
systemctl restart fail2ban
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

# 3. Hide the nginx version.
echo 'server_tokens off;' > /etc/nginx/conf.d/verquoai-hardening.conf
nginx -t
systemctl reload nginx

# 4. Move the secrets encryption key out of data/ (next to the files it protects)
#    into .env. A root-only copy is kept in /root in case .env is lost.
if [ -f "$APP/data/.user_config.key" ] && ! grep -q '^USER_CONFIG_ENCRYPTION_KEY=.' "$APP/.env"; then
    key="$(tr -d '[:space:]' < "$APP/data/.user_config.key")"
    if grep -q '^USER_CONFIG_ENCRYPTION_KEY=' "$APP/.env"; then
        sed -i "s|^USER_CONFIG_ENCRYPTION_KEY=.*|USER_CONFIG_ENCRYPTION_KEY=$key|" "$APP/.env"
    else
        echo "USER_CONFIG_ENCRYPTION_KEY=$key" >> "$APP/.env"
    fi
    install -m 600 "$APP/data/.user_config.key" /root/verquoai-user-config.key
    shred -u "$APP/data/.user_config.key"
    echo ">>> Encryption key moved to $APP/.env (backup: /root/verquoai-user-config.key)"
fi
chmod 600 "$APP/.env"

# 5. Daily backup of data/ at 01:30 UTC, kept 14 days. SQLite files are copied
#    with .backup so a write in progress cannot corrupt the copy.
cat > /usr/local/sbin/verquoai-backup <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
ROOT=/var/backups/verquoai
DEST="$ROOT/$(date +%F)"
mkdir -p "$DEST"
chmod 700 "$ROOT"
cd /opt/verquoai/app
rsync -a --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
      --exclude='.user_config.key' --exclude='historical/' data/ "$DEST/data/"
find data -name '*.db' -print0 | while IFS= read -r -d '' db; do
    mkdir -p "$DEST/$(dirname "$db")"
    sqlite3 "$db" ".backup '$DEST/$db'"
done
find "$ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
SCRIPT
chmod 700 /usr/local/sbin/verquoai-backup
echo '30 1 * * * root /usr/local/sbin/verquoai-backup' > /etc/cron.d/verquoai-backup

# 6. Rotate the app's log files.
cat > /etc/logrotate.d/verquoai <<'CONF'
/opt/verquoai/app/logs/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
CONF

systemctl restart verquoai
echo ">>> Hardening done."
[ -f /var/run/reboot-required ] && echo ">>> Kernel/security updates need a reboot: run 'reboot' when convenient (GhostLock fix)."
exit 0
