"""Admin panel: /admin and /api/admin/*.

Security model
* Only Trading IDs listed in ADMIN_USERS (.env) are admins, and only in a real
  signed-in session (never a demo). Everyone else gets 404: the panel does not
  reveal that it exists.
* Every admin API call - reads included, they return emails and access
  requests - needs an OTP confirmed in the last 15 minutes (step-up), on top of
  the session cookie. State-changing calls also pass the CSRF check in
  webapp.auth_middleware and a per-admin write limit.
* API secrets are accepted over HTTPS, stored encrypted (UserConfigService)
  and never returned, logged or audited; API keys are shown masked.
* Every change is written to the admin audit trail (who, what, target, when,
  hashed IP), without secret values.
* Changing a user's credentials, or removing them, ends that user's running
  trading services and saved broker session, so stale credentials never keep
  trading. Access-level changes apply to the very next order (access is read
  per order). An admin cannot remove an admin account or the account that
  serves shared market data.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, time as dtime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.api.service import TradingServiceManager
from src.api.shared_market import SharedMarketHub, shared_market_id
from src.auth.access import LEVEL_LABELS, LEVELS, VIEW
from src.auth.otp_auth import otp_auth
from src.auth.user_config import UserConfigService
from src.demo.sessions import is_demo_id, shutdown_service, user_demo_id
from src.metrics.store import CONTACT_STATUSES, IST, metrics
from src.utils.app_settings import set_setting
from src.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter()

TRADING_ID = re.compile(r"^[A-Z0-9]{4,10}$")
API_CREDENTIAL = re.compile(r"^[A-Za-z0-9]{6,64}$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
KITE_SESSION_RESET = dtime(6, 0)  # Kite access tokens expire around 06:00 IST


def admin_ids() -> set[str]:
    raw = os.environ.get("ADMIN_USERS", "")
    return {part for part in (p.strip().upper() for p in raw.split(",")) if TRADING_ID.match(part)}


def _web() -> Any:
    from src.api import webapp  # imported lazily: webapp imports this module

    return webapp


def _session_token(request: Request) -> str:
    return request.cookies.get(_web().SESSION_COOKIE, "")


def is_admin_session(request: Request) -> bool:
    """Listed in ADMIN_USERS, signed in for real (not a demo) and still an
    existing account - removing the account ends admin rights at once."""
    zerodha_id = otp_auth.get_session_user(_session_token(request)) or ""
    return (bool(zerodha_id) and not is_demo_id(zerodha_id) and zerodha_id in admin_ids()
            and UserConfigService.get_instance().get_user(zerodha_id) is not None)


def _admin_id(request: Request) -> str:
    """The signed-in admin's ID, or 404 for anyone else."""
    if not is_admin_session(request):
        raise HTTPException(404, "Not Found")
    return otp_auth.get_session_user(_session_token(request)) or ""


def require_admin(request: Request) -> str:
    admin = _admin_id(request)
    if not otp_auth.has_recent_step_up(_session_token(request)):
        raise HTTPException(403, "step_up_required")
    return admin


def _require_write(request: Request) -> str:
    admin = require_admin(request)
    if not otp_auth.check_ip_rate_limit(f"admin-write:{admin}", _web()._client_ip(request), max_requests=120):
        raise HTTPException(429, "Too many changes in a short time. Please wait a little.")
    return admin


def _audit(request: Request, admin: str, action: str, target: str = "", **detail: Any) -> None:
    metrics.add_audit(admin, action, target, detail, _web()._client_ip(request))
    logger.info("admin_action", admin=admin, action=action, target=target)


def _trading_id(value: Any) -> str:
    zerodha_id = str(value or "").strip().upper()
    if not TRADING_ID.match(zerodha_id):
        raise HTTPException(400, "Trading ID must be 4-10 letters or digits.")
    return zerodha_id


def _path_id(value: str) -> str:
    zerodha_id = value.strip().upper()
    if not TRADING_ID.match(zerodha_id):
        raise HTTPException(404, "Not Found")
    return zerodha_id


def _email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if not EMAIL.match(email):
        raise HTTPException(400, "Enter a valid email address.")
    return email


def _credentials(body: dict[str, Any], required: bool) -> tuple[str, str]:
    api_key = str(body.get("api_key") or "").strip()
    api_secret = str(body.get("api_secret") or "").strip()
    if not api_key and not api_secret and not required:
        return "", ""
    if not API_CREDENTIAL.match(api_key) or not API_CREDENTIAL.match(api_secret):
        raise HTTPException(400, "API key and API secret must both be given (letters and digits only).")
    return api_key, api_secret


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "Invalid request")
    return body


def _users() -> UserConfigService:
    """The user store, re-read from disk first so CLI edits are never overwritten."""
    store = UserConfigService.get_instance()
    try:
        store.reload()
    except Exception as e:
        logger.error("admin_users_reload_failed", error=str(e))
        raise HTTPException(500, "Could not read the user list. Nothing was changed.")
    return store


def _save_guard(action: Any) -> Any:
    try:
        return action()
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e).strip("'"))
    except Exception as e:
        logger.error("admin_save_failed", error=str(e))
        raise HTTPException(500, "Could not save the change. Nothing was changed.")


def _kite_session_file(zerodha_id: str) -> str:
    return f".kite_session_{zerodha_id}.json"


def _kite_session_state(zerodha_id: str) -> dict[str, Any]:
    path = _kite_session_file(zerodha_id)
    if not os.path.exists(path):
        return {"linked": False, "saved_at": ""}
    saved = datetime.fromtimestamp(os.path.getmtime(path), IST)
    now = datetime.now(IST)
    reset = datetime.combine(now.date(), KITE_SESSION_RESET, IST)
    if now < reset:
        reset -= timedelta(days=1)
    return {"linked": saved >= reset, "saved_at": saved.isoformat(timespec="minutes")}


async def _retire(zerodha_id: str, forget_broker_session: bool) -> None:
    """Stop the user's running services (real, demo, shared-market view) so the
    next request starts fresh with the current settings."""
    mgr = TradingServiceManager.get_instance()
    for uid in (zerodha_id, user_demo_id(zerodha_id), shared_market_id(zerodha_id)):
        if mgr.has_service(uid):
            await shutdown_service(mgr.get_service(uid))
            mgr.remove_service(uid)
    if forget_broker_session and os.path.exists(_kite_session_file(zerodha_id)):
        os.remove(_kite_session_file(zerodha_id))


# ── page ────────────────────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    admin = _admin_id(request)
    web = _web()
    return web.templates.TemplateResponse(request, "admin.html", {
        "request": request,
        "admin_id": admin,
        "csrf_token": otp_auth.get_csrf_token(_session_token(request)),
        "css_version": web._static_version("css/dashboard.css"),
        "terminal_css_version": web._static_version("css/terminal.css"),
        "admin_js_version": web._static_version("js/admin.js"),
        "admin_perf_js_version": web._static_version("js/admin_performance.js"),
        "secure_fetch_js_version": web._static_version("js/secure_fetch.js"),
    })


# ── read ────────────────────────────────────────────────────────────────────

@router.get("/api/admin/overview")
async def admin_overview(request: Request) -> dict[str, Any]:
    require_admin(request)
    store = _users()
    admins = admin_ids()
    users = []
    for row in store.list_users():
        uc = store.get_user(row["zerodha_id"])
        has_key = bool(uc and uc.api_key and uc.api_secret)
        users.append({
            "zerodha_id": row["zerodha_id"],
            "email": row["email"],
            "access_level": row["access_level"],
            "access_label": LEVEL_LABELS.get(row["access_level"], row["access_level"]),
            "is_admin": row["zerodha_id"] in admins,
            "has_api_key": has_key,
            "api_key_hint": f"…{uc.api_key[-4:]}" if has_key else "",
            "kite_session": _kite_session_state(row["zerodha_id"]),
            "active_sessions": otp_auth.count_sessions(row["zerodha_id"]),
        })
    hub = SharedMarketHub.get_instance()
    return {
        "users": users,
        "levels": [{"value": lv, "label": LEVEL_LABELS[lv]} for lv in LEVELS],
        "market_data": {
            "account": hub.account_id,
            "available": hub.available,
            "feed_connected": hub.feed_connected,
            "source": hub.source_kind(),          # "primary", "backup" (no broker login today) or None
            "recorder": _recorder_status(),         # OI + flow history recording (src/api/market_recorder.py)
            "candidates": [u["zerodha_id"] for u in users if u["has_api_key"]],
        },
        "backup_quotes": {**_backup().status(), "mode": _source_mode(),
                          "modes": [{"value": k, "label": v} for k, v in _source_modes().items()]},
        "contacts": metrics.list_contacts(100),
        "contact_statuses": list(CONTACT_STATUSES),
        "audit": metrics.list_audit(100),
    }


# ── users ───────────────────────────────────────────────────────────────────

@router.post("/api/admin/users")
async def admin_add_user(request: Request) -> dict[str, Any]:
    admin = _require_write(request)
    body = await _json(request)
    zerodha_id = _trading_id(body.get("zerodha_id"))
    email = _email(body.get("email"))
    level = str(body.get("access_level") or VIEW)
    if level not in LEVELS:
        raise HTTPException(400, "Choose an access level.")
    api_key, api_secret = _credentials(body, required=level != VIEW)
    store = _users()
    if store.get_user(zerodha_id):
        raise HTTPException(409, f"{zerodha_id} already exists.")
    _save_guard(lambda: store.add_user(zerodha_id, api_key, api_secret, email, level))
    contact_id = body.get("contact_id")
    if isinstance(contact_id, int):
        metrics.set_contact_status(contact_id, "approved")
    _audit(request, admin, "user_added", zerodha_id, access_level=level, with_api_key=bool(api_key),
           from_contact=contact_id if isinstance(contact_id, int) else None)
    return {"success": True}


@router.post("/api/admin/users/{zerodha_id}/access")
async def admin_set_access(zerodha_id: str, request: Request) -> dict[str, Any]:
    admin = _require_write(request)
    zerodha_id = _path_id(zerodha_id)
    level = str((await _json(request)).get("access_level") or "")
    if level not in LEVELS:
        raise HTTPException(400, "Choose an access level.")
    store = _users()
    uc = store.get_user(zerodha_id)
    if not uc:
        raise HTTPException(404, "No such user.")
    if level != VIEW and not (uc.api_key and uc.api_secret):
        raise HTTPException(400, "Add this account's API key and secret before giving it more than view access.")
    previous = uc.access_level
    _save_guard(lambda: store.set_access_level(zerodha_id, level))
    _audit(request, admin, "access_changed", zerodha_id, previous=previous, new=level)
    return {"success": True}


@router.post("/api/admin/users/{zerodha_id}/credentials")
async def admin_set_credentials(zerodha_id: str, request: Request) -> dict[str, Any]:
    admin = _require_write(request)
    zerodha_id = _path_id(zerodha_id)
    api_key, api_secret = _credentials(await _json(request), required=True)
    store = _users()
    if not store.get_user(zerodha_id):
        raise HTTPException(404, "No such user.")
    _save_guard(lambda: store.set_credentials(zerodha_id, api_key, api_secret))
    await _retire(zerodha_id, forget_broker_session=True)  # the old Kite session belongs to the old key
    _audit(request, admin, "credentials_changed", zerodha_id, api_key_hint=f"…{api_key[-4:]}")
    return {"success": True}


@router.post("/api/admin/users/{zerodha_id}/email")
async def admin_set_email(zerodha_id: str, request: Request) -> dict[str, Any]:
    admin = _require_write(request)
    zerodha_id = _path_id(zerodha_id)
    email = _email((await _json(request)).get("email"))
    store = _users()
    if not store.get_user(zerodha_id):
        raise HTTPException(404, "No such user.")
    _save_guard(lambda: store.set_email(zerodha_id, email))
    _audit(request, admin, "email_changed", zerodha_id)
    return {"success": True}


@router.post("/api/admin/users/{zerodha_id}/revoke-sessions")
async def admin_revoke_sessions(zerodha_id: str, request: Request) -> dict[str, Any]:
    admin = _require_write(request)
    zerodha_id = _path_id(zerodha_id)
    if zerodha_id == admin:
        raise HTTPException(400, "Use 'Logout all devices' for your own account.")
    ended = otp_auth.logout_all(zerodha_id)
    await _retire(zerodha_id, forget_broker_session=False)
    _audit(request, admin, "sessions_revoked", zerodha_id, sessions=ended)
    return {"success": True, "sessions_ended": ended}


@router.delete("/api/admin/users/{zerodha_id}")
async def admin_remove_user(zerodha_id: str, request: Request) -> dict[str, Any]:
    admin = _require_write(request)
    zerodha_id = _path_id(zerodha_id)
    if zerodha_id in admin_ids():
        raise HTTPException(400, "Admin accounts cannot be removed here. Take the ID out of ADMIN_USERS first.")
    if zerodha_id == SharedMarketHub.get_instance().account_id:
        raise HTTPException(400, "This account serves shared market data. Choose another source first.")
    store = _users()
    if not store.get_user(zerodha_id):
        raise HTTPException(404, "No such user.")
    _save_guard(lambda: store.remove_user(zerodha_id))
    ended = otp_auth.logout_all(zerodha_id)
    await _retire(zerodha_id, forget_broker_session=True)
    _audit(request, admin, "user_removed", zerodha_id, sessions_ended=ended)
    return {"success": True}


# ── shared market data ──────────────────────────────────────────────────────

@router.post("/api/admin/market-data")
async def admin_set_market_data(request: Request) -> dict[str, Any]:
    admin = _require_write(request)
    raw = str((await _json(request)).get("account_id") or "").strip().upper()
    if raw:
        raw = _trading_id(raw)
        uc = _users().get_user(raw)
        if not uc or not (uc.api_key and uc.api_secret):
            raise HTTPException(400, "The market data account must be a user with an API key and secret.")
    hub = SharedMarketHub.get_instance()
    previous = hub.account_id
    _save_guard(lambda: set_setting("market_data_account", raw))
    await hub.set_account(raw)
    _audit(request, admin, "market_data_account_changed", raw or "(off)", previous=previous or "(off)")
    return {"success": True}


def _recorder_status() -> dict[str, Any]:
    from src.api.market_recorder import status

    return status()


# ── backup market data (src/api/backup_quotes.py) ──────────────────────────

def _backup() -> Any:
    from src.api.backup_quotes import backup_quotes

    return backup_quotes()


def _source_mode() -> str:
    from src.api.backup_quotes import source_mode

    return source_mode()


def _source_modes() -> dict[str, str]:
    from src.api.backup_quotes import SOURCE_MODES

    return SOURCE_MODES


@router.post("/api/admin/backup-quotes")
async def admin_set_backup_quotes(request: Request) -> dict[str, Any]:
    """Backup quote source: {token?: str, clear_token?: bool, base_url?: str, enabled?: bool}.
    The token is stored encrypted and never returned."""
    from src.auth.user_config import encrypt_secret

    admin = _require_write(request)
    body = await _json(request)
    changes = []
    token = str(body.get("token") or "").strip()
    if token:
        if len(token) > 200 or any(c.isspace() for c in token):
            raise HTTPException(400, "That does not look like an API access token.")
        _save_guard(lambda: set_setting("backup_quotes_token", encrypt_secret(token)))
        changes.append("token")
    elif body.get("clear_token"):
        _save_guard(lambda: set_setting("backup_quotes_token", ""))
        changes.append("token cleared")
    if "base_url" in body:
        url = str(body.get("base_url") or "").strip().rstrip("/")
        if url and not re.fullmatch(r"https://[a-z0-9.-]+\.kotaksecurities\.com", url):
            raise HTTPException(400, "Base URL must be an https://….kotaksecurities.com address.")
        _save_guard(lambda: set_setting("backup_quotes_base_url", url))
        changes.append("base URL")
    if "enabled" in body:
        _save_guard(lambda: set_setting("backup_quotes_enabled", "1" if body.get("enabled") else "0"))
        changes.append("on" if body.get("enabled") else "off")
    if "mode" in body:
        mode = str(body.get("mode") or "")
        if mode not in _source_modes():
            raise HTTPException(400, "Unknown source order.")
        _save_guard(lambda: set_setting("quote_source_mode", mode))
        changes.append(f"order: {mode}")
    _audit(request, admin, "backup_quotes_changed", "backup", changes=", ".join(changes) or "nothing")
    return {"success": True, "status": _backup().status()}


@router.post("/api/admin/backup-quotes/test")
async def admin_test_backup_quotes(request: Request) -> dict[str, Any]:
    """Fetch a few live quotes through the backup: shows what it returns, field by field."""
    admin = _require_write(request)
    backup = _backup()
    if not backup.token():
        raise HTTPException(400, "Add the backup API access token first.")
    try:
        await backup._ensure_master()
        fut = sorted(k for k in backup._fut if k.startswith("RELIANCE") and k.endswith("FUT"))
        keys = ["NSE:NIFTY 50", "NSE:RELIANCE"] + [f"NFO:{k}" for k in fut[:1]]   # a future: shows its OI
        queries = [q for q in (backup.query_for(k) for k in keys) if q]
        raw = await backup.fetch_raw(queries)
        quotes = await backup.get_quote(keys)
    except Exception as e:
        _audit(request, admin, "backup_quotes_test", "backup", ok=False)
        return {"success": False, "error": str(e)[:300], "status": backup.status()}
    _audit(request, admin, "backup_quotes_test", "backup", ok=True)
    return {
        "success": True,
        "fields": sorted({k for d in raw for k in d}),
        "quotes": {k: {"last_price": q.last_price, "volume": q.volume, "oi": q.oi, "change": q.net_change,
                       "ohlc": q.ohlc.model_dump(), "depth_levels": len(q.depth.buy)} for k, q in quotes.items()},
        "status": backup.status(),
    }


# ── access requests ─────────────────────────────────────────────────────────

@router.post("/api/admin/contacts/{contact_id}/status")
async def admin_contact_status(contact_id: int, request: Request) -> Any:
    admin = _require_write(request)
    status = str((await _json(request)).get("status") or "")
    if status not in CONTACT_STATUSES:
        raise HTTPException(400, "Unknown status.")
    if not metrics.set_contact_status(contact_id, status):
        return JSONResponse({"detail": "No such request."}, status_code=404)
    _audit(request, admin, "contact_status_changed", str(contact_id), status=status)
    return {"success": True}


# ── strategy performance (src/analysis/performance_report.py) ───────────────
PERFORMANCE_WINDOWS = (30, 60, 90, 120)
_REPORT_FILE = re.compile(r"^performance-\d{4}-\d{2}-\d{2}\.md$")


def _window(days: int) -> int:
    if days not in PERFORMANCE_WINDOWS:
        raise HTTPException(400, f"days must be one of {', '.join(map(str, PERFORMANCE_WINDOWS))}")
    return days


async def _performance(days: int) -> dict[str, Any]:
    import asyncio

    from src.analysis import performance_report as perf

    report = await asyncio.to_thread(perf.report_for, days)
    report["findings"] = perf.findings(report)
    report["windows"] = list(PERFORMANCE_WINDOWS)
    return report


@router.get("/api/admin/performance")
async def admin_performance(request: Request, days: int = 60) -> dict[str, Any]:
    """Day Trade and Swing setups: per setup / side, per day, live board, parameters."""
    require_admin(request)
    return await _performance(_window(days))


@router.get("/api/admin/performance.md")
async def admin_performance_markdown(request: Request, days: int = 60) -> Any:
    """The same report as a Markdown download."""
    from fastapi.responses import Response

    from src.analysis import performance_report as perf

    admin = require_admin(request)
    report = await _performance(_window(days))
    _audit(request, admin, "performance_report_downloaded", f"{days} days")
    return Response(perf.markdown(report), media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="verquoai-performance-{report["today"]}-{days}d.md"',
        "Cache-Control": "no-store"})


@router.post("/api/admin/performance/email")
async def admin_performance_email(request: Request) -> dict[str, Any]:
    """Email the report (Markdown attached) to the signed-in admin's own address."""
    import asyncio

    from src.analysis import performance_report as perf
    from src.utils.mailer import send_mail

    admin = _require_write(request)
    days = _window(int((await _json(request)).get("days") or 60))
    user = _users().get_user(admin)
    if not user or not user.email:
        raise HTTPException(400, "Your account has no email address.")
    report = await _performance(days)
    md = perf.markdown(report)
    text = (f"VerquoAI strategy performance, last {days} days to {report['today']}.\n\n"
            + "\n".join(f"- {f}" for f in report["findings"]) + "\n\nThe full report is attached as Markdown.\n")
    sent = await asyncio.to_thread(send_mail, user.email, f"VerquoAI strategy performance — {report['today']} ({days} days)",
                                   text, None, None, [(f"verquoai-performance-{report['today']}-{days}d.md", md, "markdown")])
    _audit(request, admin, "performance_report_emailed", f"{days} days", sent=sent)
    if not sent:
        raise HTTPException(502, "The email could not be sent (check the SMTP settings).")
    return {"success": True, "to": user.email}


@router.get("/api/admin/performance/archive")
async def admin_performance_archive(request: Request) -> dict[str, Any]:
    """The nightly reports kept on the server (newest first)."""
    from src.analysis import performance_report as perf

    require_admin(request)
    try:
        names = sorted((n for n in os.listdir(perf.REPORT_DIR) if _REPORT_FILE.match(n)), reverse=True)
    except OSError:
        names = []
    return {"reports": [{"name": n, "date": n[12:22]} for n in names]}


@router.get("/api/admin/performance/archive/{name}")
async def admin_performance_archive_file(name: str, request: Request) -> Any:
    from fastapi.responses import FileResponse

    from src.analysis import performance_report as perf

    admin = require_admin(request)
    if not _REPORT_FILE.match(name):
        raise HTTPException(404, "Not Found")
    path = os.path.join(perf.REPORT_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "Not Found")
    _audit(request, admin, "performance_report_downloaded", name)
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=name,
                        headers={"Cache-Control": "no-store"})
