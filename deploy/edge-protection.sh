#!/usr/bin/env bash
# nginx-level throttling and bot protection for verquoai.com. Run as root:
#   sudo bash /opt/verquoai/app/deploy/edge-protection.sh
# Safe to re-run. Rolls back if nginx rejects the configuration.
#
# - Rate limits (429 before a request reaches Python):
#     login / OTP / contact / demo start  10 per minute per IP (burst 10)
#     usage metrics                       30 per minute per IP (burst 20)
#     everything else                     20 per second per IP (burst 80)
#   Kite's order postback (/api/auth/postback) is never strictly limited.
# - At most 40 open connections per IP.
# - Requests for the bare IP address or unknown host names are dropped (444 /
#   TLS handshake refused), so scanners never reach the app.
# - fail2ban bans IPs that keep hitting the limits (1 hour).
set -euo pipefail

LIMITS=/etc/nginx/conf.d/verquoai-limits.conf
CATCHALL=/etc/nginx/conf.d/verquoai-catchall.conf
backup=$(mktemp -d)
cp -a "$LIMITS" "$CATCHALL" "$backup"/ 2>/dev/null || true

cat > "$LIMITS" <<'CONF'
# Managed by deploy/edge-protection.sh
limit_req_status 429;
limit_conn_status 429;

map $uri $vq_sensitive_key {
    default "";
    ~^/auth/                 $binary_remote_addr;
    ~^/api/auth/callback$    $binary_remote_addr;
    ~^/contact$              $binary_remote_addr;
    ~^/demo/start$           $binary_remote_addr;
}
map $uri $vq_metrics_key {
    default "";
    /metrics/events          $binary_remote_addr;
}
map $uri $vq_general_key {
    default                  $binary_remote_addr;
    /api/auth/postback       "";
}

limit_req_zone $vq_sensitive_key zone=vq_sensitive:10m rate=10r/m;
limit_req_zone $vq_metrics_key   zone=vq_metrics:10m   rate=30r/m;
limit_req_zone $vq_general_key   zone=vq_general:20m   rate=20r/s;
limit_conn_zone $binary_remote_addr zone=vq_conn:10m;

limit_req zone=vq_sensitive burst=10 nodelay;
limit_req zone=vq_metrics burst=20 nodelay;
limit_req zone=vq_general burst=80 nodelay;
limit_conn vq_conn 40;
CONF

cat > "$CATCHALL" <<'CONF'
# Managed by deploy/edge-protection.sh - requests for any host other than
# verquoai.com (bare IP, scanners, spoofed Host headers) never reach the app.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    return 444;
}
server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;
    ssl_reject_handshake on;
}
CONF

if ! nginx -t; then
    echo ">>> nginx rejected the new config - rolling back" >&2
    rm -f "$LIMITS" "$CATCHALL"
    cp -a "$backup"/* /etc/nginx/conf.d/ 2>/dev/null || true
    nginx -t && systemctl reload nginx
    exit 1
fi
systemctl reload nginx

apt-get install -y fail2ban >/dev/null
cat > /etc/fail2ban/jail.d/verquoai-nginx.local <<'CONF'
[nginx-limit-req]
enabled  = true
port     = http,https
logpath  = /var/log/nginx/error.log
maxretry = 30
findtime = 10m
bantime  = 1h
CONF
systemctl restart fail2ban
echo ">>> Edge protection active."
fail2ban-client status nginx-limit-req | tail -3 || true
