# Deploying verquoai.com

Production: Ubuntu 24.04 VPS `97.74.84.127`, app in `/opt/verquoai/app` (owned by the `verquo` user), virtualenv in `/opt/verquoai/venv`, systemd unit `verquoai`, nginx in front, Let's Encrypt TLS.

- First install: `sudo bash deploy/setup.sh https://github.com/deputygmit-rgb/algotrader.git <email>` (also runs `apt-get upgrade`, certbot and the firewall).
- Server hardening: `sudo bash /opt/verquoai/app/deploy/harden.sh <admin-user>`.
- Routine updates: the steps below.

## 1. On your Mac: put the release on `main`

```bash
cd ~/algotrader
git status                                  # must be clean
git checkout main
git pull --ff-only origin main
git merge --ff-only fix/deploy-runtime      # or whichever branch holds the release
git push origin main
```

## 2. On the server: back up, pull, install, restart

```bash
ssh <admin-user>@97.74.84.127

cd /opt/verquoai/app
sudo tar czf /opt/verquoai/backup-$(date +%Y%m%d-%H%M).tgz -C /opt/verquoai/app data .env .kite_session_*.json 2>/dev/null; ls -lh /opt/verquoai/backup-*.tgz | tail -1

sudo -u verquo git -C /opt/verquoai/app pull --ff-only
sudo -u verquo /opt/verquoai/venv/bin/pip install -r /opt/verquoai/app/requirements.txt
sudo cp /opt/verquoai/app/deploy/verquoai.service /etc/systemd/system/verquoai.service
sudo systemctl daemon-reload
sudo systemctl restart verquoai
```

`git` and `pip` run as `verquo` because it owns the files; as root, git refuses ("dubious ownership"). If `git pull` asks for a password, use the credential the server was set up with (deploy key or token).

## 3. Settings in `.env`

Compare with `.env.template` after each release: `diff <(grep -o '^[A-Z_]*=' .env.template | sort) <(sudo grep -o '^[A-Z_]*=' .env | sort)` (lines starting `<` are settings missing from `.env`). Edit with `sudo -u verquo nano /opt/verquoai/app/.env`, then `sudo systemctl restart verquoai`.

| Setting | Purpose |
|---|---|
| `PUBLIC_URL=https://verquoai.com` | Site address used in links in outgoing email. |
| `MARKET_DATA_ACCOUNT=` | Zerodha ID whose Kite session serves live market data to view-only accounts. Empty turns it off. Check Kite Connect / exchange data terms first. |
| `ADMIN_EMAIL=` | Receives access requests. |
| `USER_CONFIG_ENCRYPTION_KEY=` | Never change it: existing `data/users.json` would become unreadable. |

## Edge protection (once, and after nginx changes)

`sudo bash /opt/verquoai/app/deploy/edge-protection.sh` installs nginx rate limits (login/OTP/contact/demo 10 per minute per IP, metrics 30 per minute, everything else 20 per second), a 40-connection cap per IP, a catch-all server that drops requests for the bare IP or unknown host names, and a fail2ban jail for repeat offenders. It rolls back if `nginx -t` fails. Check bans with `sudo fail2ban-client status nginx-limit-req`.

## 4. Check

```bash
systemctl status verquoai --no-pager
sudo cat /proc/$(systemctl show -p MainPID --value verquoai)/environ | tr '\0' '\n' | grep '^TZ='   # TZ=Asia/Kolkata
sudo tail -n 20 /opt/verquoai/app/logs/trading.log
curl -sI https://verquoai.com | head -1                                                            # HTTP/2 200
```

If it does not start: `sudo journalctl -u verquoai -n 50 --no-pager`.

## Users and access levels

Run from `/opt/verquoai/app`, then restart the service:

```bash
cd /opt/verquoai/app
sudo -u verquo /opt/verquoai/venv/bin/python scripts/manage_users.py list
sudo -u verquo /opt/verquoai/venv/bin/python scripts/manage_users.py add AB1234 trader@example.com --access view
sudo -u verquo /opt/verquoai/venv/bin/python scripts/manage_users.py access AB1234 trade
sudo -u verquo /opt/verquoai/venv/bin/python scripts/manage_users.py update AB1234
sudo -u verquo /opt/verquoai/venv/bin/python scripts/manage_users.py remove AB1234
sudo systemctl restart verquoai
```

- `view`: no real orders. `trade`: orders the user places by hand. `agentic`: also automated orders (live strategies, auto-trade, agents).
- New accounts default to `view`; accounts created before access levels existed are `agentic`.
- A `view` account may skip the Kite API key (press Enter). It then sees the shared live market data, or the demo market when `MARKET_DATA_ACCOUNT` is empty or not logged in to Kite that day.
- Raising an account above `view` needs its own Kite API key (`update` first).

## Shared market view, daily

The account in `MARKET_DATA_ACCOUNT` must log in to Kite each trading day (Connect Zerodha in the dashboard). Until it does, view-only users see the demo market. The shared feed uses one extra Kite WebSocket on that account's API key (Kite allows 3).

## Rollback

```bash
cd /opt/verquoai/app
sudo -u verquo git log --oneline -5
sudo -u verquo git checkout <previous-commit>
sudo systemctl restart verquoai
```

To restore data: `sudo systemctl stop verquoai && sudo tar xzf /opt/verquoai/backup-<stamp>.tgz -C /opt/verquoai/app && sudo chown -R verquo:verquo /opt/verquoai/app && sudo systemctl start verquoai`. Return to normal updates with `sudo -u verquo git checkout main`.
