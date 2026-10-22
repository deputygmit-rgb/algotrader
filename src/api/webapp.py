from __future__ import annotations

# Before anything reads the clock or logs: run on exchange time (IST) and
# configure logging. Under systemd uvicorn imports this module directly, so
# main.py's setup is not on that path.
from src.utils.clock import use_exchange_timezone
from src.utils.logger import setup_logging

use_exchange_timezone()
setup_logging()

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote, urlsplit

from fastapi.middleware.gzip import GZipMiddleware
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.routing import Match

from src.agent.mcp_driver import MCPDriverSupervisorAgent
from src.analysis.nifty_contribution import DemoQuoteSimulator, compute_nifty_contribution
from src.analysis.fno_flow import DemoFnoSimulator, FlowHistory, compute_fno_flow
from src.api.shared_market import SharedMarketHub, shared_market_id
from src.auth.access import AGENTIC, LEVEL_LABELS, VIEW, AccessDenied
from src.api.day_trade import DayTradeTracker, completed_session_bars
from src.api.market_data import build_account_summary
from src.api.request_limiter import api_limiter
from src.utils.memory import run_hygiene
from src.analysis.efficacy import run_rebuilder
from src.api.response_cache import shared_json
from src.api.service import TradingService, TradingServiceManager
from src.auth.otp_auth import SESSION_EXPIRY_SECONDS, STEP_UP_PURPOSE, otp_auth, rate_limit_subject
from src.auth.user_config import UserConfigService
from src.metrics.store import device_of, metrics
from src.utils.mailer import mail_guard, send_mail
from src.api.admin import is_admin_session, router as admin_router
from src.demo.sessions import (
    MAX_PER_NETWORK as DEMO_MAX_PER_NETWORK,
    PUBLIC_SESSION_SECONDS,
    demo_sessions,
    is_public_demo_id,
    new_public_id,
    shutdown_service,
    user_demo_id,
)
from src.api.static_files import MinifiedStaticFiles
from src.api.demo_scope import LOCKED_MESSAGE, public_demo_blocked
from src.utils.exceptions import KiteError, KillSwitchError
from src.utils.logger import get_logger

logger = get_logger(__name__)

mcp_driver = MCPDriverSupervisorAgent.get_instance()
_nifty_demo = DemoQuoteSimulator()
_fno_demo: DemoFnoSimulator | None = None
_fno_demo_history = FlowHistory()


def _get_fno_demo() -> DemoFnoSimulator:
    global _fno_demo
    if _fno_demo is None:
        _fno_demo = DemoFnoSimulator()
        _fno_demo.seed_history(_fno_demo_history)
    return _fno_demo


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    await mcp_driver.startup()
    demo_sessions.purge_orphans()
    reaper = asyncio.create_task(demo_sessions.run_reaper())
    viewer_reaper = asyncio.create_task(_run_viewer_reaper())
    flusher = asyncio.create_task(metrics.run_flusher())
    hygiene = asyncio.create_task(run_hygiene())        # gc + return freed memory to the OS
    efficacy = asyncio.create_task(run_rebuilder())     # setups' rolling track record, nightly
    resume = asyncio.create_task(_resume_accounts())    # trading picks up again after a restart
    try:
        yield
    finally:
        resume.cancel()
        reaper.cancel()
        viewer_reaper.cancel()
        hygiene.cancel()
        efficacy.cancel()
        flusher.cancel()
        await asyncio.to_thread(metrics.flush)
        await mcp_driver.shutdown()


async def _resume_accounts() -> None:
    """After the process restarts (crash, out-of-memory kill, deploy), accounts that were
    trading - or have orders / positions in flight - are picked up again at once, instead
    of waiting for someone to press Start Live (see ``TradingService.resume_after_restart``).
    One without a broker session resumes when it next logs in."""
    await asyncio.sleep(2)
    try:
        from src.data.market_data_store import get_market_data_store

        accounts = await asyncio.to_thread(get_market_data_store().engine_accounts)
    except Exception as e:
        logger.error("resume_accounts_failed", error=str(e))
        return
    for zid in accounts:
        if zid.startswith(("DEMO-", "VIEW-")):
            continue
        user_cfg = UserConfigService.get_instance().get_user(zid)
        if not user_cfg:
            continue
        try:
            svc = TradingServiceManager.get_instance().get_service(zid, api_key=user_cfg.api_key,
                                                                   api_secret=user_cfg.api_secret)
            result = await svc.resume_after_restart()
            logger.warning("account_resume_after_restart", zerodha_id=zid, **result)
        except Exception as e:
            logger.error("account_resume_failed", zerodha_id=zid, error=str(e))


# No /docs, /redoc or /openapi.json: they would publish the whole API (and its
# internal descriptions) to every signed-in user; nothing in the app uses them.
app = FastAPI(title="VerquoAI", version="2.0", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
@app.exception_handler(AccessDenied)
async def _access_denied(request: Request, exc: AccessDenied) -> JSONResponse:
    """An action the account's access level does not allow (src/auth/access.py)."""
    logger.warning("access_denied", path=request.url.path, reason=str(exc))
    return JSONResponse({"success": False, "error": str(exc), "access_denied": True}, status_code=403)


# The market-flow snapshot covers ~500 stocks and is polled every few seconds.
app.add_middleware(GZipMiddleware, minimum_size=2048)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# scripts are served minified (src/api/static_files.py); nginx sends /static/js/ here for that
app.mount("/static", MinifiedStaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

SESSION_COOKIE = "kta_session"

PUBLIC_PATHS = {"/", "/login", "/demo/start", "/auth/send-otp", "/auth/verify-otp", "/auth/callback",
                "/api/auth/callback", "/api/auth/postback", "/metrics/events", "/contact",
                "/robots.txt", "/sitemap.xml"}
# the only pages search engines may index; everything else is sent with noindex
INDEXABLE_PATHS = {"/", "/robots.txt", "/sitemap.xml"}


def _real_service(zerodha_id: str) -> TradingService:
    if is_public_demo_id(zerodha_id):
        raise HTTPException(403, "Demo sessions cannot link a trading account - sign in with your own account")
    user_cfg = UserConfigService.get_instance().get_user(zerodha_id)
    if not user_cfg:
        raise HTTPException(403, "User not configured")
    mgr = TradingServiceManager.get_instance()
    return mgr.get_service(zerodha_id, api_key=user_cfg.api_key, api_secret=user_cfg.api_secret)


def _service_for_session(token: str) -> TradingService:
    """The service a session works with.

    Public demo visitors get their own simulated-broker service. A logged-in
    user gets their real service once Zerodha is connected, and a demo service
    (same terminal, simulated broker) until then.
    """
    zerodha_id = otp_auth.get_session_user(token)
    if not zerodha_id:
        raise HTTPException(401, "Not authenticated")
    mgr = TradingServiceManager.get_instance()
    if is_public_demo_id(zerodha_id):
        demo_sessions.touch(zerodha_id)
        return mgr.get_service(zerodha_id, demo=True)
    real = _real_service(zerodha_id)
    if real.is_authenticated:
        return real
    shared = _shared_market_service(zerodha_id, real)
    if shared is not None:
        return shared
    demo_id = user_demo_id(zerodha_id)
    demo_sessions.touch(demo_id)
    return mgr.get_service(demo_id, demo=True)


def _shared_market_service(zerodha_id: str, real: TradingService) -> TradingService | None:
    """View-only accounts read live market data from the shared source account
    while it is logged in to Zerodha (src/api/shared_market.py)."""
    if real.access_level != VIEW or not SharedMarketHub.get_instance().available_for(zerodha_id):
        return None
    uid = shared_market_id(zerodha_id)
    _viewer_seen[uid] = time.time()
    return TradingServiceManager.get_instance().get_service(uid, shared_market=True)


# A view-only user's service (and its agents) is retired after this long without
# a request; the next request starts a fresh one. An open dashboard tab polls
# every few seconds (at least once a minute in a background tab), so only
# closed tabs are retired.
VIEWER_IDLE_SECONDS = int(os.environ.get("VIEWER_IDLE_MINUTES", "30")) * 60
_viewer_seen: dict[str, float] = {}


async def _retire_shared_market(uid: str) -> None:
    """Stop a shared-market (view-only) service: agents, tasks, feed subscriptions."""
    _viewer_seen.pop(uid, None)
    mgr = TradingServiceManager.get_instance()
    if not mgr.has_service(uid):
        return
    svc = mgr.get_service(uid, shared_market=True)
    try:
        await svc._ticker.disconnect()          # leave the hub's shared feed
    except Exception as e:
        logger.warning("shared_market_stop_failed", error=str(e))
    await shutdown_service(svc)
    mgr.remove_service(uid)


async def _end_shared_market(zerodha_id: str) -> None:
    """The user connected their own Zerodha account: drop their shared-market session."""
    await _retire_shared_market(shared_market_id(zerodha_id))


def _has_open_sockets(svc: TradingService) -> bool:
    oi = getattr(svc, "oi_tracker", None)
    return bool(getattr(svc, "_ws_clients", None) or getattr(oi, "_ws_clients", None))


async def reap_idle_viewers() -> int:
    cutoff = time.time() - VIEWER_IDLE_SECONDS
    mgr, reaped = TradingServiceManager.get_instance(), 0
    for uid, seen in list(_viewer_seen.items()):
        if seen >= cutoff:
            continue
        if not mgr.has_service(uid):
            _viewer_seen.pop(uid, None)
            continue
        if _has_open_sockets(mgr.get_service(uid, shared_market=True)):
            continue                            # a tab is still connected
        await _retire_shared_market(uid)
        logger.info("viewer_service_retired", viewer=uid)
        reaped += 1
    return reaped


async def _run_viewer_reaper(every_seconds: int = 60) -> None:
    while True:
        await asyncio.sleep(every_seconds)
        try:
            await reap_idle_viewers()
        except Exception as e:
            logger.error("viewer_reaper_failed", error=str(e))


def get_user_service(request: Request) -> TradingService:
    return _service_for_session(request.cookies.get(SESSION_COOKIE, ""))


def get_real_user_service(request: Request) -> TradingService:
    """The user's Zerodha-backed service, for connecting Zerodha itself."""
    zerodha_id = get_session_zerodha_id(request)
    if not zerodha_id:
        raise HTTPException(401, "Not authenticated")
    return _real_service(zerodha_id)


def _is_public_demo(request: Request) -> bool:
    cookies = getattr(request, "cookies", None) or {}
    return is_public_demo_id(otp_auth.get_session_user(cookies.get(SESSION_COOKIE, "")) or "")


def demo_kind(svc: TradingService) -> str:
    """'public', 'user' (not connected to Zerodha yet) or '' for a real service."""
    if not getattr(svc, "is_demo", False):
        return ""
    return "public" if is_public_demo_id(svc.zerodha_id) else "user"


def get_session_zerodha_id(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE, "")
    return otp_auth.get_session_user(token) or ""


def is_logged_in(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    return otp_auth.validate_session(token)


def _same_origin(websocket: WebSocket) -> bool:
    """Browsers send Origin on WebSocket handshakes; refuse other sites' pages
    (cross-site WebSocket hijacking) even if a session cookie came along."""
    origin = websocket.headers.get("origin")
    if not origin:
        return True  # non-browser clients; the session cookie still decides
    return urlsplit(origin).netloc.lower() == (websocket.headers.get("host") or "").lower()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _dev_otp_fallback_enabled(request: Request) -> bool:
    if os.environ.get("APP_ENV", "").strip().lower() != "development":
        return False
    if os.environ.get("OTP_DEV_FALLBACK_ENABLED", "").strip().lower() != "true":
        return False
    # Behind a reverse proxy every peer looks like loopback; never trust that.
    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
        return False
    client_ip = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(client_ip).is_loopback
    except ValueError:
        return False


def _is_secure_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


def _public_error(e: Exception) -> str:
    """Message safe to show in the browser.

    Kite/risk errors and validation errors describe the user's own request and
    are shown as-is; anything else (file paths, SQL, library internals) is
    logged and replaced with a generic message.
    """
    if isinstance(e, (KiteError, ValueError)):
        return str(e)[:300]
    logger.error("request_failed", error_type=type(e).__name__, error=str(e))
    return "Internal error - the details are in the server log"


def _content_security_policy(request: Request) -> str:
    # The templates use inline <script> blocks and onclick= handlers, so
    # script-src needs 'unsafe-inline'. The policy still blocks scripts from
    # other origins, sending data to other origins, <base>/<object> injection,
    # form posts elsewhere and framing.
    ws_origin = f"wss://{request.url.netloc}" if _is_secure_request(request) else f"ws://{request.url.netloc}"
    return "; ".join([
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com data:",
        "img-src 'self' data: https:",
        f"connect-src 'self' {ws_origin}",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


def _apply_security_headers(response, request: Request):
    response.headers["Content-Security-Policy"] = _content_security_policy(request)
    if request.url.path == "/admin" or request.url.path.startswith("/api/admin/"):
        # personal data and account settings: never cached, never indexed
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    elif request.url.path not in INDEXABLE_PATHS and not request.url.path.startswith("/static/"):
        # the terminal, its API and the sign-in page stay out of search results
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if _is_secure_request(request):
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


CSRF_EXEMPT_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    response = None

    if path != "/robots.txt" and _AI_CRAWLER.search(request.headers.get("user-agent", "")):
        # robots.txt asks them to stay away; this is for the ones that do not ask
        response = PlainTextResponse("Crawling for AI training is not permitted.", status_code=403)
    elif path.startswith("/static/") or path in PUBLIC_PATHS:
        response = await call_next(request)
    elif path.startswith("/ws/"):
        token = request.cookies.get(SESSION_COOKIE, "")
        if not otp_auth.validate_session(token):
            response = JSONResponse({"error": "Not authenticated"}, status_code=401)
        else:
            response = await call_next(request)
    elif not is_logged_in(request):
        if path.startswith("/api/"):
            response = JSONResponse({"error": "Not authenticated"}, status_code=401)
        elif path != "/login":
            response = RedirectResponse("/login", status_code=302)
        else:
            response = await call_next(request)
    elif path.startswith("/api/") and not api_limiter.allow(
            otp_auth.get_session_user(request.cookies.get(SESSION_COOKIE, "")) or "?"):
        # per account, on top of nginx's per-IP limit (src/api/request_limiter.py)
        response = JSONResponse({"error": "Too many requests - please slow down"}, status_code=429,
                                headers={"Retry-After": "2"})
    elif path.startswith("/api/") and public_demo_blocked(path) and _is_public_demo(request):
        # Market Flow, the boards and Analysis are for accounts with access (src/api/demo_scope.py)
        response = JSONResponse({"error": LOCKED_MESSAGE, "demo_locked": True}, status_code=403)
    elif request.method not in CSRF_EXEMPT_METHODS:
        token = request.cookies.get(SESSION_COOKIE, "")
        csrf_header = request.headers.get("X-CSRF-Token", "")
        if not otp_auth.verify_csrf_token(token, csrf_header):
            response = JSONResponse({"error": "CSRF validation failed"}, status_code=403)
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)

    return _apply_security_headers(response, request)


VISITOR_COOKIE = "vq_vid"
_VISITOR_ID = re.compile(r"^[a-f0-9]{32}$")
_UNTRACKED_PATHS = ("/static/", "/favicon.ico", "/metrics/events")


def _session_identity(request: Any) -> tuple[str, str]:
    """(user id, kind) behind the session cookie: kind is user / demo / anon."""
    zerodha_id = otp_auth.get_session_user(request.cookies.get(SESSION_COOKIE, "")) or ""
    if not zerodha_id:
        return "", "anon"
    return zerodha_id, "demo" if is_public_demo_id(zerodha_id) else "user"


def _route_template(request: Request) -> str:
    """Route pattern for a request (``/app/stock/{symbol}``), also for requests the
    auth middleware answered before routing; unknown paths share one bucket so
    scanners probing random URLs cannot flood the table."""
    route = request.scope.get("route")
    if route is not None:
        return route.path
    scope = {"type": "http", "path": request.url.path, "method": request.method, "root_path": ""}
    for candidate in app.router.routes:
        if candidate.matches(scope)[0] != Match.NONE:
            return getattr(candidate, "path", "(unmatched)")
    return "(unmatched)"


def _track(request: Any, event: str, **detail: Any) -> None:
    """Record a business event; never lets metrics break the request."""
    try:
        user_id, kind = _session_identity(request)
        if detail.get("user_id"):
            user_id, kind = str(detail.pop("user_id")), detail.pop("kind", kind)
        state = getattr(request, "state", None)
        metrics.record_event(
            event, visitor_id=getattr(state, "visitor_id", "") if state else "", user_id=user_id, kind=kind,
            page=request.url.path if hasattr(request, "url") else "", detail=detail, ip=_client_ip(request),
            device=device_of(request.headers.get("user-agent", "")),
        )
    except Exception:
        pass


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """Counts feature usage per user and route, and gives visitors an anonymous id."""
    path = request.url.path
    if path.startswith(_UNTRACKED_PATHS[:2]):
        return await call_next(request)
    visitor = request.cookies.get(VISITOR_COOKIE, "")
    new_visitor = not _VISITOR_ID.match(visitor)
    if new_visitor:
        visitor = secrets.token_hex(16)
    request.state.visitor_id = visitor
    started = time.perf_counter()
    response = await call_next(request)
    if path not in _UNTRACKED_PATHS:
        try:
            route = _route_template(request)
            user_id, kind = _session_identity(request)
            if device_of(request.headers.get("user-agent", "")) == "bot":
                kind = "bot"
            metrics.record_request(user_id or kind, kind, route, request.method, response.status_code,
                                   (time.perf_counter() - started) * 1000)
        except Exception:
            pass
    if new_visitor:
        response.set_cookie(VISITOR_COOKIE, visitor, max_age=365 * 86400, httponly=True, samesite="lax",
                            secure=_is_secure_request(request), path="/")
    return response


@app.post("/metrics/events")
async def metrics_events(request: Request) -> Any:
    """Browser activity (page views, clicks, tab switches) from static/js/metrics.js."""
    ip = _client_ip(request)
    if not otp_auth.check_ip_rate_limit("metrics", ip, max_requests=360, window_seconds=3600):
        return JSONResponse({"ok": False}, status_code=429)
    raw = await request.body()
    if len(raw) > 32 * 1024:
        return JSONResponse({"ok": False}, status_code=413)
    try:
        body = json.loads(raw)
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list):
        return JSONResponse({"ok": False}, status_code=400)
    user_id, kind = _session_identity(request)
    device = device_of(request.headers.get("user-agent", ""))
    if device == "bot":  # crawlers and scripts: accept, but don't store their clicks
        return {"ok": True}
    visitor = getattr(request.state, "visitor_id", "")
    for e in events[:50]:
        if isinstance(e, dict):
            metrics.record_event(
                str(e.get("event", "")), visitor_id=visitor, user_id=user_id, kind=kind, page=e.get("page", ""),
                target=e.get("target", ""), detail=e.get("detail") if isinstance(e.get("detail"), dict) else None,
                ip=ip, device=device,
            )
    return {"ok": True}


_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(value: Any, limit: int, multiline: bool = False) -> str:
    text = _CONTROL.sub("", str(value or ""))
    if not multiline:
        text = text.replace("\n", " ")
    return text.strip()[:limit]


# What a requester can ask for (keys are stored; labels go in the emails).
ACCESS_LEVELS: dict[str, str] = {
    "view": "View live trading - read-only dashboards, signals and analysis; no orders",
    "trade": "Trading - place and manage your own orders through your broker",
    "agentic": "Agentic trading - AI agents place trades for you within limits you set",
}
# The broker is asked as free text: public pages do not name the broker we
# integrate with. This recognises it from what the requester typed.
SUPPORTED_BROKER = re.compile(r"zerodha|kite", re.IGNORECASE)
SUPPORTED_BROKER_LABEL = "Your broker - supported (API connection)"   # emails never name the broker


def _public_url() -> str:
    """Site address used in outgoing email. Configured, never taken from the request's
    Host header (which a client controls), so links in our emails cannot be spoofed."""
    return (os.environ.get("PUBLIC_URL") or "https://verquoai.com").rstrip("/")


# What the requester should expect, per access level (what they asked for; the
# level is confirmed when the workspace is set up).
_ACCESS_FOLLOW_UP: dict[str, str] = {
    "view": (
        "You asked for read-only access: live dashboards, signals, Market Flow, stock\n"
        "analysis, backtests and paper trading. View-only accounts cannot place real orders -\n"
        "this is enforced on every order. You can ask to upgrade your access at any time."
    ),
    "trade": (
        "You asked for trading access: you place and manage your own orders through your\n"
        "broker account. Every order is checked against risk limits (daily loss, position\n"
        "size, exposure) before it is sent, and the Kill Switch in your workspace stops all\n"
        "trading at once. Orders placed automatically by strategies or agents need agentic\n"
        "trading access."
    ),
    "agentic": (
        "You asked for agentic trading: AI agents place trades for you. Agentic trading stays\n"
        "OFF until you switch it on. Before enabling it you set how much to risk per trade, a\n"
        "maximum order value and a maximum number of orders per day; the agents trade only\n"
        "within those limits. You can switch it off, or use the Kill Switch, at any time.\n"
        "We recommend reviewing the strategies' backtest and paper-trading results first."
    ),
}


def _access_confirmation(contact_id: int, access_level: str, supported: bool) -> str:
    """Follow-up email to the requester: summary, next steps and what to prepare.

    Fixed text only - none of the visitor's free-text fields - so the form cannot
    be used to send arbitrary content to someone else's address."""
    site = _public_url()
    if supported:
        prepare = (
            "WHAT TO PREPARE\n"
            "  1. An active account with your broker. Your Trading ID is your broker client ID.\n"
            "  2. An API (developer) app from your broker's developer portal - we will send\n"
            "     the exact steps (it is a paid subscription billed by your broker; live market\n"
            "     data for your workspace comes through it). When creating the app, enter:\n"
            f"       Redirect URL:  {site}/api/auth/callback\n"
            f"       Postback URL:  {site}/api/auth/postback   (optional)\n"
            "  3. Keep the app's API key and API secret ready. Please do NOT send the API\n"
            "     secret by email - we will arrange a secure way for you to enter it.\n"
            "  4. Your broker requires a fresh login every trading day: once set up, use\n"
            "     \"Link trading account\" in your workspace each morning before trading.\n"
        )
        platform_line = SUPPORTED_BROKER_LABEL
    else:
        prepare = (
            "ABOUT YOUR BROKER\n"
            "  We currently connect one broker. We have noted the broker you use and will let\n"
            "  you know if we add support for it. If you also hold an account with another\n"
            "  broker, reply to this email and we will tell you whether it can be connected.\n"
        )
        platform_line = "Another broker - we will confirm whether it can be connected"
    return (
        "Hello,\n\n"
        "Thank you for your interest in VerquoAI. We have received your request.\n\n"
        "REQUEST SUMMARY\n"
        f"  Reference:         #{contact_id}\n"
        f"  Access requested:  {ACCESS_LEVELS[access_level]}\n"
        f"  Trading platform:  {platform_line}\n\n"
        "ABOUT THE ACCESS YOU CHOSE\n"
        + "".join(f"  {line}\n" for line in _ACCESS_FOLLOW_UP[access_level].split("\n"))
        + "\n"
        "WHAT HAPPENS NEXT\n"
        + (
            "  1. We review your request and reply from this address with your setup details.\n"
            "  2. We set up your private workspace, linked to your Trading ID.\n"
            f"  3. You sign in at {site} with your Trading ID and email address. Each sign-in\n"
            "     uses a one-time code sent to your email - there is no password to remember.\n\n"
            if supported else
            "  1. We review your request and reply from this address.\n"
            "  2. A live workspace needs a broker account we can connect (see below); until then\n"
            "     the demo below shows everything VerquoAI does.\n\n"
        )
        + prepare
        + "\n"
        "IN THE MEANTIME\n"
        f"  - Explore the full workspace on a simulated market: open {site} and choose\n"
        "    \"Try the demo\" (no account needed, nothing real at stake).\n"
        "  - Questions? Simply reply to this email.\n\n"
        f"Please quote #{contact_id} in any reply. If you did not make this request, you can\n"
        "ignore this email - nothing will be set up without you.\n\n"
        "Important: trading involves substantial risk, including possible loss of capital.\n"
        "VerquoAI provides analytical and workflow tools, not investment advice.\n\n"
        "- The VerquoAI team\n"
    )


FORM_TOKEN_MIN_AGE = 3            # seconds: faster than this is a script
FORM_TOKEN_MAX_AGE = 3 * 86400


def _form_token(now: Optional[float] = None) -> str:
    """Signed render time for the contact form (no cookie or session needed)."""
    ts = str(int(now if now is not None else time.time()))
    sig = hmac.new(otp_auth._secret.encode(), f"contact-form:{ts}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{ts}.{sig}"


def _form_token_state(token: str) -> str:
    """'ok', 'fast' (bot), or 'invalid' (missing, forged or older than three days)."""
    ts, _, sig = token.partition(".")
    if not ts.isdigit() or not hmac.compare_digest(_form_token(int(ts)), token):
        return "invalid"
    age = time.time() - int(ts)
    if age < FORM_TOKEN_MIN_AGE:
        return "fast"
    return "ok" if age <= FORM_TOKEN_MAX_AGE else "invalid"


@app.post("/contact")
async def contact(request: Request) -> Any:
    """Access requests from prospective customers (landing page and demo)."""
    ip = _client_ip(request)
    if not otp_auth.check_ip_rate_limit("contact", ip, max_requests=5, window_seconds=3600):
        return JSONResponse({"success": False, "message": "Too many requests. Please try again later."}, status_code=429)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Invalid request"}, status_code=400)
    thanks = {"success": True, "message": "Thanks! We'll get back to you by email."}
    if body.get("website"):  # hidden field only bots fill in
        return thanks
    token_state = _form_token_state(str(body.get("form_token") or ""))
    if token_state == "fast":
        # Faster than a person fills the form. Say so rather than fake success:
        # a real (very quick) person must not lose their request silently.
        return JSONResponse({"success": False, "message": "Please take a moment to check your details, then send again."},
                            status_code=429)
    if token_state != "ok":
        return JSONResponse({"success": False, "message": "This form has expired. Please reload the page and try again."},
                            status_code=400)
    name = _clean(body.get("name"), 80)
    email = _clean(body.get("email"), 120).lower()
    phone = _clean(body.get("phone"), 20)
    trading_id = _clean(body.get("trading_id"), 10).upper()
    message = _clean(body.get("message"), 1000, multiline=True)
    source = body.get("source") if body.get("source") in ("landing", "login", "demo") else "landing"
    access_level = str(body.get("access_level") or "")
    platform = _clean(body.get("platform"), 60)
    if not name:
        return JSONResponse({"success": False, "message": "Please tell us your name."}, status_code=400)
    if not _EMAIL.match(email):
        return JSONResponse({"success": False, "message": "Please enter a valid email address."}, status_code=400)
    if phone and not re.fullmatch(r"[0-9+\-() ]{6,20}", phone):
        return JSONResponse({"success": False, "message": "Please enter a valid phone number."}, status_code=400)
    if trading_id and not re.fullmatch(r"[A-Z0-9]{4,10}", trading_id):
        return JSONResponse({"success": False, "message": "Trading ID should be 4-10 letters or digits."}, status_code=400)
    if access_level not in ACCESS_LEVELS:
        return JSONResponse({"success": False, "message": "Please choose the access you want."}, status_code=400)
    if not platform:
        return JSONResponse({"success": False, "message": "Please tell us which broker you trade with."}, status_code=400)
    if body.get("confirm") not in (True, "yes", "on", "true"):
        return JSONResponse({"success": False, "message": "Please confirm the access and platform you selected."},
                            status_code=400)
    supported = bool(SUPPORTED_BROKER.search(platform))

    user_id, kind = _session_identity(request)
    visitor = getattr(request.state, "visitor_id", "")
    contact_id = await asyncio.to_thread(
        metrics.add_contact, name=name, email=email, phone=phone, trading_id=trading_id, message=message,
        source=source, visitor_id=visitor, user_id=user_id, ip=ip,
        access_level=access_level, platform=platform,
    )
    _track(request, "contact_submitted", source=source)
    logger.info("contact_request_received", contact_id=contact_id, source=source)

    admin = os.environ.get("ADMIN_EMAIL", "").strip()
    if admin and mail_guard.allow_admin_notice(email):
        text = (
            f"New access request #{contact_id} from the {source}{' (demo session)' if kind == 'demo' else ''}.\n\n"
            f"Name:       {name}\nEmail:      {email}\nPhone:      {phone or '-'}\nTrading ID: {trading_id or '-'}\n\n"
            f"Access requested: {ACCESS_LEVELS[access_level]}\n"
            f"Trading platform: {platform}"
            + (" - supported\n" if supported else " - NOT supported yet (only the integrated broker can be connected)\n")
            + "  The requester confirmed both choices.\n\n"
            f"Message:\n{message or '-'}\n\n"
            f"Review and add the user in the admin panel: {_public_url()}/admin\n"
            "Or on the server:\n"
            f"  sudo -u verquo /opt/verquoai/venv/bin/python scripts/manage_users.py add {trading_id or '<TRADING_ID>'} {email} --access {access_level}\n"
        )
        if await asyncio.to_thread(send_mail, admin, f"VerquoAI access request: {name}", text, email):
            await asyncio.to_thread(metrics.mark_emailed, contact_id)
    # tell the requester what they asked for, so they have a record of it - at most
    # once a day per address, within a daily budget (see MailGuard)
    copied = mail_guard.allow_requester_copy(email) and await asyncio.to_thread(
        send_mail, email, f"Your VerquoAI access request #{contact_id} - next steps",
        _access_confirmation(contact_id, access_level, supported), admin or None,
    )
    if copied:
        return {**thanks, "message": "Thanks! We've emailed you a copy of your request and will get back to you."}
    return thanks



@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if is_logged_in(request):
        return RedirectResponse("/app", status_code=302)
    return templates.TemplateResponse(request, "landing.html", {"request": request, "open_login": True, "site_url": _public_url(),
                                                                "form_token": _form_token()})


@app.post("/auth/send-otp")
async def send_otp(request: Request) -> dict[str, Any]:
    ip = _client_ip(request)
    if not otp_auth.check_ip_rate_limit("send-otp", ip, max_requests=15, window_seconds=3600):
        return {"success": False, "message": "Too many requests. Please try again later."}

    body = await request.json()
    email = body.get("email", "").strip().lower()
    zerodha_id = body.get("zerodha_id", "").strip().upper()
    if not email:
        return {"success": False, "message": "Email is required"}
    if not zerodha_id:
        return {"success": False, "message": "Trading ID is required"}

    user_cfg_svc = UserConfigService.get_instance()
    user = user_cfg_svc.get_user(zerodha_id)
    if not user or not user.email or user.email.strip().lower() != email:
        return {"success": False, "message": "Account details could not be verified"}
    # Per (account, IP): someone who knows your ID and email cannot use up the
    # account-wide allowance from one address and lock you out.
    if not otp_auth.check_ip_rate_limit(f"send-otp:{zerodha_id}", ip, max_requests=5, window_seconds=3600):
        return {"success": False, "message": "Too many requests. Please try again later."}

    try:
        otp = otp_auth.generate_otp(zerodha_id)
    except Exception as e:
        return {"success": False, "message": _public_error(e)}
    
    if not otp_auth.send_otp_email(otp, user.email, zerodha_id):
        if _dev_otp_fallback_enabled(request):
            logger.warning("otp_email_failed_dev_fallback_enabled", zerodha_id=zerodha_id)
            return {
                "success": True,
                "message": "Email delivery failed; development-only OTP fallback is active.",
                "mode": "development",
                "otp": otp,
            }
        otp_auth.discard_pending_otp(zerodha_id)
        return {
            "success": False,
            "message": "Unable to deliver OTP. Check email configuration and try again.",
        }
    _track(request, "otp_sent", user_id=zerodha_id, kind="user")
    return {"success": True, "message": "OTP sent to your registered email"}


@app.post("/auth/verify-otp")
async def verify_otp(request: Request) -> Any:
    ip = _client_ip(request)
    if not otp_auth.check_ip_rate_limit("verify-otp", ip, max_requests=30, window_seconds=3600):
        return {"success": False, "message": "Too many attempts. Please try again later."}

    body = await request.json()
    otp = body.get("otp", "").strip()
    zerodha_id = body.get("zerodha_id", "").strip().upper()
    if not otp:
        return {"success": False, "message": "OTP is required"}
    if not zerodha_id:
        return {"success": False, "message": "Trading ID is required"}

    session_token = otp_auth.verify_otp(otp, zerodha_id)
    if session_token:
        response = JSONResponse({"success": True, "message": "Login successful", "zerodha_id": zerodha_id})
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            max_age=SESSION_EXPIRY_SECONDS,
            httponly=True,
            samesite="lax",
            secure=_is_secure_request(request),
            path="/",
        )
        _track(request, "login_success", user_id=zerodha_id, kind="user")
        return response
    else:
        _track(request, "login_failed", user_id=zerodha_id, kind="user")
        return {"success": False, "message": "Invalid or expired OTP"}


DEMO_TOKEN_MIN_AGE = 1           # seconds between the page loading and the demo starting


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _demo_start_refusal(request: Request, body: dict[str, Any]) -> tuple[str, str, int] | None:
    """Why a demo start looks scripted: (reason, message, status), or None.

    A demo costs a simulated market and four agents, so it is only started from
    our own page, by a browser, at least a second after that page was rendered
    (the page's signed ``form_token``)."""
    if device_of(request.headers.get("user-agent", "")) == "bot":
        return "bot_agent", "The demo opens in a web browser.", 403
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return "cross_site", "Open the demo from verquoai.com.", 403
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
        return "cross_origin", "Open the demo from verquoai.com.", 403
    token = str(body.get("form_token") or "")
    ts, _, _ = token.partition(".")
    state = _form_token_state(token)
    if state == "fast" and ts.isdigit() and time.time() - int(ts) >= DEMO_TOKEN_MIN_AGE:
        state = "ok"
    if state == "fast":
        return "too_fast", "One moment - starting the demo…", 429
    if state != "ok":
        return "no_token", "This page has expired. Please reload it and try again.", 400
    return None


@app.post("/demo/start")
async def demo_start(request: Request) -> Any:
    """Open the terminal on a simulated market and broker, no account needed."""
    if is_logged_in(request):
        return {"success": True, "redirect": "/app"}
    refusal = _demo_start_refusal(request, await _json_body(request))
    if refusal:
        logger.info("demo_start_refused", reason=refusal[0], ip=_client_ip(request))
        return JSONResponse({"success": False, "message": refusal[1]}, status_code=refusal[2])
    if not otp_auth.check_ip_rate_limit("demo-start", _client_ip(request), max_requests=6, window_seconds=3600):
        return JSONResponse({"success": False, "message": "Too many demo sessions from this network. Try again later."},
                            status_code=429)
    network = rate_limit_subject(_client_ip(request))
    if demo_sessions.active_for_network(network) >= DEMO_MAX_PER_NETWORK:
        return JSONResponse({"success": False, "message": "You already have demo sessions open. Close one, or try again later."},
                            status_code=429)
    if not demo_sessions.has_capacity():
        return JSONResponse({"success": False, "message": "The demo is busy right now. Please try again in a few minutes."},
                            status_code=503)
    demo_id = new_public_id()
    token = otp_auth.create_demo_session(demo_id, PUBLIC_SESSION_SECONDS)
    demo_sessions.register(demo_id, network)
    logger.info("demo_session_started", demo_id=demo_id)
    _track(request, "demo_start", user_id=demo_id, kind="demo")
    response = JSONResponse({"success": True, "redirect": "/app"})
    response.set_cookie(SESSION_COOKIE, token, max_age=PUBLIC_SESSION_SECONDS, httponly=True, samesite="lax",
                        secure=_is_secure_request(request), path="/")
    return response


@app.get("/auth/csrf")
async def csrf_token(request: Request) -> Any:
    """The CSRF token for the session this browser has now. A page loaded before
    a new sign-in (another tab, an expired session) refreshes its stale token
    here; ``zerodha_id`` lets it notice the session belongs to another account.
    Same-origin only: no CORS headers, so other sites cannot read it."""
    token = request.cookies.get(SESSION_COOKIE, "")
    response = JSONResponse({"csrf_token": otp_auth.get_csrf_token(token),
                             "zerodha_id": otp_auth.get_session_user(token) or ""})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/auth/logout")
async def logout(request: Request) -> Any:
    token = request.cookies.get(SESSION_COOKIE, "")
    zerodha_id = otp_auth.get_session_user(token) or ""
    _track(request, "demo_exit" if is_public_demo_id(zerodha_id) else "logout")
    if is_public_demo_id(zerodha_id):
        await demo_sessions.end(zerodha_id)
    otp_auth.logout(token)
    response = JSONResponse({"success": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.post("/auth/logout-all")
async def logout_all(request: Request) -> Any:
    """End this user's sessions on every browser and device."""
    zerodha_id = get_session_zerodha_id(request)
    count = otp_auth.logout_all(zerodha_id) if zerodha_id else 0
    response = JSONResponse({"success": True, "sessions_ended": count})
    response.delete_cookie(SESSION_COOKIE)
    return response


STEP_UP_REQUIRED = "step_up_required"


def _require_step_up(request: Request, svc: TradingService) -> None:
    """Risky actions (real orders, live trading, auto-trade, kill switch off)
    need an OTP confirmed in the last few minutes, not just a session cookie.
    Demo services trade a simulated sandbox and view-only services can place
    no orders, so neither needs a confirmation."""
    if getattr(svc, "is_demo", False):
        return
    # view-only sessions on shared market data can place no order at all
    # (SharedMarketClient refuses every order call): streaming prices is not risky
    if getattr(svc, "is_shared_market", False):
        return
    if not otp_auth.has_recent_step_up(request.cookies.get(SESSION_COOKIE, "")):
        raise HTTPException(403, STEP_UP_REQUIRED)


@app.post("/auth/step-up/send")
async def step_up_send(request: Request) -> dict[str, Any]:
    zerodha_id = get_session_zerodha_id(request)
    user = UserConfigService.get_instance().get_user(zerodha_id) if zerodha_id else None
    if not user or not user.email:
        raise HTTPException(403, "User not configured")
    if not otp_auth.check_ip_rate_limit(f"step-up:{zerodha_id}", _client_ip(request), max_requests=10):
        return {"success": False, "message": "Too many requests. Please try again later."}
    try:
        otp = otp_auth.generate_otp(zerodha_id, purpose=STEP_UP_PURPOSE)
    except Exception as e:
        return {"success": False, "message": _public_error(e)}
    if not otp_auth.send_otp_email(otp, user.email, zerodha_id):
        otp_auth.discard_pending_otp(zerodha_id, purpose=STEP_UP_PURPOSE)
        return {"success": False, "message": "Unable to deliver OTP. Check email configuration and try again."}
    return {"success": True, "message": "Confirmation code sent to your registered email"}


@app.post("/auth/step-up/verify")
async def step_up_verify(request: Request) -> dict[str, Any]:
    if not otp_auth.check_ip_rate_limit("step-up-verify", _client_ip(request), max_requests=30):
        return {"success": False, "message": "Too many attempts. Please try again later."}
    body = await request.json()
    otp = str(body.get("otp", "")).strip()
    if otp and otp_auth.verify_step_up(request.cookies.get(SESSION_COOKIE, ""), otp):
        _track(request, "step_up_verified")
        return {"success": True}
    return {"success": False, "message": "Invalid or expired code"}


def _static_version(rel_path: str) -> str:
    try:
        return str(int(os.path.getmtime(os.path.join(BASE_DIR, "static", rel_path))))
    except OSError:
        return "1"


# nginx caches /static for days: templates version their assets with
# {{ static_version('js/x.js') }} so a deploy reaches browsers immediately.
templates.env.globals["static_version"] = _static_version


@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "landing.html", {"request": request, "open_login": False, "site_url": _public_url(),
                                                                "form_token": _form_token()})


# Crawlers that collect pages to train AI models: refused everywhere (the product's
# text and screens are ours). Search engines, including AI search, may read the
# landing page only.
AI_TRAINING_CRAWLERS = ("GPTBot", "CCBot", "Google-Extended", "ClaudeBot", "anthropic-ai", "Bytespider",
                        "Applebot-Extended", "meta-externalagent", "FacebookBot", "Diffbot", "ImagesiftBot",
                        "omgili", "cohere-ai", "AI2Bot", "Timpibot", "img2dataset")
_AI_CRAWLER = re.compile("|".join(re.escape(b) for b in AI_TRAINING_CRAWLERS), re.IGNORECASE)


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt() -> str:
    site = _public_url()
    blocked = "".join(f"User-agent: {bot}\n" for bot in AI_TRAINING_CRAWLERS)
    return (f"{blocked}Disallow: /\n\n"
            "User-agent: *\nAllow: /$\nAllow: /static/css/\nAllow: /static/img/\nAllow: /static/favicon.svg\n"
            "Allow: /static/js/landing.js\nDisallow: /\n\n"
            f"Sitemap: {site}/sitemap.xml\n")


@app.get("/sitemap.xml")
async def sitemap_xml() -> Response:
    site = _public_url()
    landing = os.path.join(BASE_DIR, "templates", "landing.html")
    updated = datetime.fromtimestamp(os.path.getmtime(landing)).date().isoformat()
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"  <url><loc>{site}/</loc><lastmod>{updated}</lastmod><changefreq>weekly</changefreq><priority>1.0</priority></url>\n"
            "</urlset>\n")
    return Response(body, media_type="application/xml")


@app.get("/app", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    svc = get_user_service(request)
    zerodha_id = get_session_zerodha_id(request)
    session_token = request.cookies.get(SESSION_COOKIE, "")
    js_path = os.path.join(BASE_DIR, "static", "js", "dashboard.js")
    try:
        js_version = str(int(os.path.getmtime(js_path)))
    except OSError:
        js_version = "1"
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "demo_kind": demo_kind(svc),
        "form_token": _form_token(),
        "is_admin": is_admin_session(request),
        "is_authenticated": svc.is_authenticated,
        "is_running": svc.is_running,
        "zerodha_id": zerodha_id,
        "csrf_token": otp_auth.get_csrf_token(session_token),
        "js_version": js_version,
        "flow_js_version": _static_version("js/market_flow.js"),
        "day_trade_js_version": _static_version("js/day_trade.js"),
        "css_version": _static_version("css/dashboard.css"),
        "terminal_css_version": _static_version("css/terminal.css"),
        "collapsible_js_version": _static_version("js/collapsible.js"),
        "terminal_js_version": _static_version("js/terminal.js"),
        "agents_js_version": _static_version("js/agents.js"),
        "contact_js_version": _static_version("js/contact.js"),
        "metrics_js_version": _static_version("js/metrics.js"),
    })


@app.get("/app/stock/{symbol}", response_class=HTMLResponse)
async def stock_detail_page(request: Request, symbol: str) -> HTMLResponse:
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    normalized_symbol = symbol.upper().strip()
    if not re.fullmatch(r"[A-Z0-9&.-]{1,32}", normalized_symbol):
        raise HTTPException(400, "Invalid stock symbol")
    session_token = request.cookies.get(SESSION_COOKIE, "")
    return templates.TemplateResponse(request, "stock_detail.html", {
        "request": request,
        "symbol": normalized_symbol,
        "csrf_token": otp_auth.get_csrf_token(session_token),
        "css_version": _static_version("css/stock_detail.css"),
        "terminal_css_version": _static_version("css/terminal.css"),
        "collapsible_js_version": _static_version("js/collapsible.js"),
        "js_version": _static_version("js/stock_detail.js"),
        "zerodha_id": get_session_zerodha_id(request),
        "metrics_js_version": _static_version("js/metrics.js"),
    })


def _access_label(request: Request, svc: TradingService) -> str:
    """The signed-in account's own level. A user exploring the demo sandbox has
    every feature there, but the label must not suggest their account does."""
    if demo_kind(svc) == "user":
        user = UserConfigService.get_instance().get_user(get_session_zerodha_id(request))
        if user:
            return f"{LEVEL_LABELS[user.access_level]} · demo: all features"
    return LEVEL_LABELS[svc.access_level]


@app.get("/api/status")
async def api_status(request: Request, full: bool = False) -> dict[str, Any]:
    svc = get_user_service(request)
    result = {
        "authenticated": svc.is_authenticated,
        "demo": demo_kind(svc),
        # live prices from the shared feed; never says whose account serves it
        "market_feed": "shared" if getattr(svc, "is_shared_market", False) else "",
        "running": svc.is_running,
        "zerodha_id": svc.zerodha_id,
        "access_level": svc.access_level,
        "access_label": _access_label(request, svc),
        "strategies": svc.get_strategies_info(),
        "risk": svc.get_risk_summary(),
        "journal": svc.get_journal_summary(),
        "execution": svc.get_execution_summary(),
        "ticks": svc.get_live_ticks(),
    }
    if svc.is_authenticated and full:
        try:
            margins = await svc.get_margins()
            result["margins"] = margins
        except Exception:
            result["margins"] = None
        try:
            positions = await svc.get_positions()
            result["positions"] = positions
        except Exception:
            result["positions"] = None
        try:
            holdings = await svc.get_holdings()
            result["holdings"] = holdings
        except Exception:
            result["holdings"] = None
        try:
            profile = await svc.get_profile()
            result["profile"] = profile
        except Exception:
            result["profile"] = None
        try:
            trades_today = await svc.get_trades_today_count()
        except Exception:
            trades_today = None
        if result["positions"] is not None or result["holdings"] is not None:
            result["account"] = build_account_summary(
                result["positions"], result["holdings"], result["margins"], trades_today
            )
    return result


@app.get("/api/login-url")
async def login_url(request: Request) -> dict[str, str]:
    user = UserConfigService.get_instance().get_user(get_session_zerodha_id(request))
    if user is not None and not user.api_key:
        raise HTTPException(400, "Your account has no broker API app yet - ask the admin to add your API key.")
    svc = get_real_user_service(request)
    return {"url": svc.get_login_url()}


@app.get("/api/auth/callback")
async def auth_callback(request: Request):
    request_token = request.query_params.get("request_token", "")
    status = request.query_params.get("status", "")
    if status != "success" or not request_token:
        return RedirectResponse("/app?auth_error=Login+failed+or+was+cancelled")
    try:
        svc = get_real_user_service(request)
        await svc.authenticate(request_token)
        await demo_sessions.end(user_demo_id(svc.zerodha_id))
        await _end_shared_market(svc.zerodha_id)
        _track(request, "account_linked")
        mcp_driver.wake_agent(svc.zerodha_id)
        return RedirectResponse("/app?auth=success")
    except HTTPException:
        return RedirectResponse("/login?auth_error=Please+login+first")
    except Exception as e:
        logger.error("auth_callback_failed", error=str(e))
        return RedirectResponse(f"/app?auth_error={quote(_public_error(e)[:200])}")


@app.get("/auth/callback")
async def auth_callback_bare(request: Request):
    """Handles Zerodha redirect to /auth/callback (without /api prefix)."""
    request_token = request.query_params.get("request_token", "")
    status = request.query_params.get("status", "")
    if status != "success" or not request_token:
        return RedirectResponse("/app?auth_error=Login+failed+or+was+cancelled")
    try:
        svc = get_real_user_service(request)
        await svc.authenticate(request_token)
        await demo_sessions.end(user_demo_id(svc.zerodha_id))
        await _end_shared_market(svc.zerodha_id)
        _track(request, "account_linked")
        mcp_driver.wake_agent(svc.zerodha_id)
        return RedirectResponse("/app?auth=success")
    except HTTPException:
        return RedirectResponse("/login?auth_error=Please+login+first")
    except Exception as e:
        logger.error("auth_callback_bare_failed", error=str(e))
        return RedirectResponse(f"/app?auth_error={quote(_public_error(e)[:200])}")


@app.get("/api/auth/postback")
async def auth_postback(request: Request):
    return {"status": "ok"}


POSTBACK_MAX_BYTES = 64 * 1024


@app.post("/api/auth/postback")
async def auth_postback_post(request: Request):
    """Kite order postback. Public URL, so only accept bodies carrying Kite's
    checksum: SHA-256(order_id + order_timestamp + api_secret)."""
    if not otp_auth.check_ip_rate_limit("postback", _client_ip(request), max_requests=1000):
        return JSONResponse({"status": "error"}, status_code=429)
    raw = await request.body()
    if len(raw) > POSTBACK_MAX_BYTES:
        return JSONResponse({"status": "error"}, status_code=413)
    try:
        body = json.loads(raw)
    except ValueError:
        return JSONResponse({"status": "error"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"status": "error"}, status_code=400)
    user = UserConfigService.get_instance().get_user(str(body.get("user_id", "")))
    checksum = str(body.get("checksum", ""))
    if not user or not user.api_secret or not checksum:
        return JSONResponse({"status": "error"}, status_code=403)
    expected = hashlib.sha256(
        f"{body.get('order_id', '')}{body.get('order_timestamp', '')}{user.api_secret}".encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(expected, checksum):
        logger.warning("postback_checksum_invalid", user_id=user.zerodha_id)
        return JSONResponse({"status": "error"}, status_code=403)
    logger.info(
        "postback_received",
        user_id=user.zerodha_id,
        order_id=str(body.get("order_id", ""))[:40],
        status=str(body.get("status", ""))[:40],
        tradingsymbol=str(body.get("tradingsymbol", ""))[:40],
    )
    return {"status": "ok"}


@app.post("/api/authenticate")
async def authenticate(request: Request) -> dict[str, Any]:
    body = await request.json()
    token = body.get("request_token", "")
    if not token:
        raise HTTPException(400, "request_token is required")
    svc = get_real_user_service(request)
    result = await svc.authenticate(token)
    await demo_sessions.end(user_demo_id(svc.zerodha_id))
    _track(request, "account_linked")
    mcp_driver.wake_agent(svc.zerodha_id)
    return result


@app.get("/api/profile")
async def profile(request: Request) -> dict[str, Any]:
    svc = get_user_service(request)
    try:
        return await svc.get_profile()
    except Exception as e:
        raise HTTPException(500, _public_error(e))


@app.get("/api/margins")
async def margins(request: Request) -> dict[str, Any]:
    svc = get_user_service(request)
    try:
        return await svc.get_margins()
    except Exception as e:
        raise HTTPException(500, _public_error(e))


@app.get("/api/orders")
async def orders(request: Request) -> list[dict[str, Any]]:
    svc = get_user_service(request)
    try:
        return await svc.get_orders()
    except Exception as e:
        raise HTTPException(500, _public_error(e))


@app.get("/api/positions")
async def positions(request: Request) -> dict[str, Any]:
    svc = get_user_service(request)
    try:
        return await svc.get_positions()
    except Exception as e:
        raise HTTPException(500, _public_error(e))


@app.get("/api/holdings")
async def holdings(request: Request) -> list[dict[str, Any]]:
    svc = get_user_service(request)
    try:
        return await svc.get_holdings()
    except Exception as e:
        raise HTTPException(500, _public_error(e))


@app.get("/api/risk")
async def risk(request: Request) -> dict[str, Any]:
    return get_user_service(request).get_risk_summary()


@app.get("/api/journal")
async def journal(request: Request) -> dict[str, Any]:
    return get_user_service(request).get_journal_summary()


@app.get("/api/signals")
async def signals(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    return get_user_service(request).get_recent_signals(limit)


@app.get("/api/strategies")
async def strategies(request: Request) -> list[dict[str, Any]]:
    return get_user_service(request).get_strategies_info()


@app.get("/api/strategies/catalog")
async def strategy_catalog(request: Request) -> list[dict[str, Any]]:
    """Every single-stock strategy available for testing / live use (built-in,
    analysis and custom), with label, group and the timeframes it is tested on."""
    return get_user_service(request).get_strategy_catalog()


@app.post("/api/strategies/add")
async def add_strategy(request: Request) -> dict[str, Any]:
    body = await request.json()
    name = body.get("name", "")
    params = body.get("params")
    svc = get_user_service(request)
    # live strategies place orders on their own
    svc.require_access(AGENTIC, "Running a strategy in live trading")
    return svc.add_strategy(name, params)


@app.post("/api/strategies/promote-from-backtest")
async def promote_strategy_from_backtest(request: Request) -> dict[str, Any]:
    body = await request.json()
    name = body.get("name", "")
    params = body.get("params")
    svc = get_user_service(request)
    svc.require_access(AGENTIC, "Running a strategy in live trading")
    return await svc.promote_backtest_to_live(name, params)


@app.post("/api/strategies/remove")
async def remove_strategy(request: Request) -> dict[str, Any]:
    body = await request.json()
    return get_user_service(request).remove_strategy(body.get("name", ""))


@app.post("/api/strategies/toggle")
async def toggle_strategy(request: Request) -> dict[str, Any]:
    body = await request.json()
    svc = get_user_service(request)
    name = body.get("name", "")
    # pausing is always allowed; resuming lets the strategy trade again
    if any(s.name == name and not s.is_active for s in svc._strategies):
        svc.require_access(AGENTIC, "Resuming a live strategy")
    return svc.toggle_strategy(name)


@app.get("/api/instruments/search")
async def search_instruments(request: Request, q: str = "", exchange: str = "NSE") -> list[dict[str, Any]]:
    svc = get_user_service(request)
    try:
        instruments = await svc.search_instruments(q, exchange)
        return instruments
    except Exception as e:
        raise HTTPException(500, _public_error(e))


@app.post("/api/live/start")
async def start_live(request: Request) -> dict[str, Any]:
    body = await request.json()
    tokens = body.get("tokens", [])
    mode = body.get("mode", "quote")
    if not tokens:
        raise HTTPException(400, "tokens list is required")
    svc = get_user_service(request)
    _require_step_up(request, svc)
    return await svc.start_live(tokens, mode)


@app.post("/api/live/stop")
async def stop_live(request: Request) -> dict[str, Any]:
    return await get_user_service(request).stop_live()


@app.get("/api/ticks")
async def live_ticks(request: Request) -> dict[int, dict[str, Any]]:
    return get_user_service(request).get_live_ticks()


@app.get("/api/options/chain")
async def option_chain(request: Request, underlying: str = "NIFTY", expiry: Optional[str] = None) -> dict[str, Any]:
    return await get_user_service(request).get_option_chain(underlying, expiry)


@app.get("/api/oi/summary")
async def oi_summary(request: Request, underlying: str = "NIFTY") -> dict[str, Any]:
    return get_user_service(request).get_oi_summary(underlying)


@app.get("/api/oi/all")
async def oi_all(request: Request) -> dict[str, Any]:
    return get_user_service(request).get_oi_all()


@app.post("/api/oi/start")
async def start_oi_tracking(request: Request) -> dict[str, Any]:
    body = await request.json()
    nifty_spot = body.get("nifty_spot", 0.0)
    sensex_spot = body.get("sensex_spot", 0.0)
    instruments = body.get("instruments")
    svc = get_user_service(request)
    return await svc.start_oi_tracking(nifty_spot, sensex_spot, instruments)


@app.websocket("/ws/oi")
async def oi_websocket(websocket: WebSocket) -> None:
    if not _same_origin(websocket):
        await websocket.close(code=4003)
        return
    try:
        svc = _service_for_session(websocket.cookies.get(SESSION_COOKIE, ""))
    except HTTPException:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    svc.oi_tracker.register_oi_ws_client(websocket)
    try:
        data = svc.get_oi_all()
        await websocket.send_json(data)
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        svc.oi_tracker.unregister_oi_ws_client(websocket)
    except Exception:
        svc.oi_tracker.unregister_oi_ws_client(websocket)


# ─── OI Deep Analysis ─────────────────────────────────────────

@app.post("/api/oi/futures/scan")
async def futures_oi_scan(request: Request) -> dict[str, Any]:
    """Run full futures OI analysis (index + stock futures)."""
    svc = get_user_service(request)
    return await svc.get_futures_oi_report()


@app.get("/api/oi/futures/report")
async def futures_oi_report(request: Request) -> dict[str, Any]:
    """Get last computed futures OI report."""
    return get_user_service(request).get_futures_oi_cached()


@app.post("/api/oi/options/scan")
async def options_oi_scan(request: Request) -> dict[str, Any]:
    """Run near-ATM options OI analysis for an index."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    underlying = body.get("underlying", "NIFTY") if body else "NIFTY"
    expiry = body.get("expiry") if body else None
    svc = get_user_service(request)
    return await svc.get_options_oi_report(underlying, expiry)


@app.get("/api/oi/options/report")
async def options_oi_report(request: Request, underlying: str = "NIFTY") -> dict[str, Any]:
    """Get last computed options OI report."""
    return get_user_service(request).get_options_oi_cached(underlying)


@app.post("/api/oi/options/compare")
async def options_oi_compare(request: Request) -> dict[str, Any]:
    """Compare current vs next expiry options OI."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    underlying = body.get("underlying", "NIFTY") if body else "NIFTY"
    svc = get_user_service(request)
    return await svc.get_options_oi_comparison(underlying)


@app.get("/api/oi/pcr-history")
async def pcr_history(request: Request, underlying: str = "NIFTY") -> list[dict[str, Any]]:
    return get_user_service(request).get_pcr_history(underlying)


@app.get("/api/oi/straddle-history")
async def straddle_history(request: Request, underlying: str = "NIFTY") -> list[dict[str, Any]]:
    return get_user_service(request).get_straddle_history(underlying)


# ── OI Strategy Routes ─────────────────────────────────────────

@app.post("/api/oi/strategy/scan")
async def oi_strategy_scan(request: Request) -> dict[str, Any]:
    """Scan NIFTY or SENSEX options OI and detect trading signals."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    underlying = body.get("underlying", "NIFTY") if body else "NIFTY"
    return await get_user_service(request).oi_strategy_scan(underlying)


@app.post("/api/oi/strategy/scan-both")
async def oi_strategy_scan_both(request: Request) -> dict[str, Any]:
    """Scan both NIFTY and SENSEX for OI signals."""
    return await get_user_service(request).oi_strategy_scan_both()


@app.post("/api/oi/strategy/execute")
async def oi_strategy_execute(request: Request) -> dict[str, Any]:
    """Execute a specific OI signal by creating a position."""
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request body"}
    signal_id = body.get("signal_id", "")
    if not signal_id:
        return {"error": "signal_id required"}
    svc = get_user_service(request)
    _require_step_up(request, svc)  # places a real broker order
    return await svc.oi_strategy_execute_signal(signal_id)


@app.post("/api/oi/strategy/close")
async def oi_strategy_close(request: Request) -> dict[str, Any]:
    """Close an OI strategy position."""
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request body"}
    position_id = body.get("position_id", "")
    exit_price = body.get("exit_price", 0.0)
    if not position_id:
        return {"error": "position_id required"}
    return await get_user_service(request).oi_strategy_close_position(position_id, exit_price)


@app.get("/api/oi/strategy/positions")
async def oi_strategy_positions(request: Request, underlying: str = "") -> dict[str, Any]:
    return get_user_service(request).oi_strategy_get_positions(underlying or None)


@app.get("/api/oi/strategy/signals")
async def oi_strategy_signals(request: Request, underlying: str = "", limit: int = 50) -> list[dict[str, Any]]:
    return get_user_service(request).oi_strategy_get_signals(underlying or None, limit)


@app.get("/api/oi/strategy/summary")
async def oi_strategy_summary(request: Request) -> dict[str, Any]:
    return get_user_service(request).oi_strategy_get_summary()


@app.get("/api/oi/strategy/config")
async def oi_strategy_config(request: Request) -> dict[str, Any]:
    return get_user_service(request).oi_strategy_get_config()


@app.post("/api/oi/strategy/config")
async def oi_strategy_update_config(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request body"}
    return get_user_service(request).oi_strategy_update_config(body)


@app.get("/api/analysis/status")
async def analysis_status(request: Request) -> dict[str, Any]:
    return get_user_service(request).get_scanner_status()


@app.post("/api/analysis/scan")
async def run_analysis_scan(request: Request) -> dict[str, Any]:
    svc = get_user_service(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    symbols = body.get("symbols") if body else None
    universe = (body.get("universe", "nifty50") if body else "nifty50")
    try:
        return await svc.run_analysis_scan(symbols, universe=universe)
    except Exception as e:
        logger.error("analysis_scan_error", error=str(e))
        raise HTTPException(500, _public_error(e))


@app.get("/api/analysis/results")
async def analysis_results(request: Request, universe: str = "nifty50") -> dict[str, Any]:
    return get_user_service(request).get_analysis_results(universe)


DEEP_PROFILE_MAX_AGE = timedelta(minutes=15)


def _deep_profile_is_fresh(profile: dict[str, Any]) -> bool:
    try:
        as_of = datetime.fromisoformat(profile.get("as_of") or "")
    except ValueError:
        return False
    return datetime.now() - as_of <= DEEP_PROFILE_MAX_AGE


@app.get("/api/analysis/deep/{symbol}")
async def analysis_deep_profile(request: Request, symbol: str) -> dict[str, Any]:
    """Get deep analysis profile for a single stock (on-demand if not cached)."""
    svc = get_user_service(request)
    sym = symbol.upper().strip()
    cached = svc.get_deep_profile(sym)
    if not cached.get("error"):
        if _deep_profile_is_fresh(cached):
            return {**cached, "cache": "hit"}
        if str(cached.get("as_of") or "")[:10] == datetime.now().strftime("%Y-%m-%d"):
            # analysed earlier today: answer now, refresh behind the scenes
            svc.refresh_analysis_in_background(sym)
            return {**cached, "cache": "stale", "refreshing": True}
    # missing or from a previous day, so never 'fresh': this refreshes it now
    fresh = await svc.analyze_stock_on_demand(sym)
    if fresh.get("error") and not cached.get("error"):
        # keep showing the last good profile, clearly flagged as stale
        return {**cached, "stale": True, "refresh_error": fresh["error"]}
    return fresh


@app.post("/api/analysis/stock/test-strategy")
async def analysis_stock_test_strategy(request: Request) -> dict[str, Any]:
    """Download 5min/15min/hourly/daily history for a stock and backtest a
    strategy on each timeframe (Analysis tab's per-stock "Test Strategy" action)."""
    body = await request.json()
    symbol = (body.get("symbol") or "").upper().strip()
    strategy_name = body.get("strategy", "")
    if not symbol:
        raise HTTPException(400, "symbol is required")
    if not strategy_name:
        raise HTTPException(400, "strategy is required")
    svc = get_user_service(request)
    try:
        return await svc.test_stock_strategy(
            symbol=symbol,
            strategy_name=strategy_name,
            exchange=body.get("exchange", "NSE"),
            strategy_params=body.get("strategy_params"),
        )
    except Exception as e:
        logger.error("stock_test_strategy_error", symbol=symbol, error=str(e))
        raise HTTPException(500, _public_error(e))


@app.post("/api/analysis/stock/execute")
async def analysis_stock_execute(request: Request) -> dict[str, Any]:
    """Place a real order for a stock that passed test-strategy on a given timeframe."""
    body = await request.json()
    symbol = (body.get("symbol") or "").upper().strip()
    strategy_name = body.get("strategy", "")
    timeframe = body.get("timeframe", "")
    if not symbol or not strategy_name or not timeframe:
        raise HTTPException(400, "symbol, strategy and timeframe are required")
    svc = get_user_service(request)
    _require_step_up(request, svc)  # places a real broker order
    try:
        return await svc.execute_stock_strategy_signal(
            symbol=symbol,
            strategy_name=strategy_name,
            timeframe=timeframe,
            exchange=body.get("exchange", "NSE"),
            quantity=int(body.get("quantity", 0)),
            atr_sl_mult=float(body.get("atr_sl_mult", 1.5)),
            atr_target_mult=float(body.get("atr_target_mult", 3.0)),
            strategy_params=body.get("strategy_params"),
            risk_amount=float(body.get("risk_amount", 1000.0)),
        )
    except Exception as e:
        logger.error("stock_execute_error", symbol=symbol, error=str(e))
        raise HTTPException(500, _public_error(e))


@app.get("/api/research/status")
async def research_status(request: Request) -> dict[str, Any]:
    """Health status of the supervised stock-research MCP agent."""
    return {"running": mcp_driver.research_agent_running}


@app.get("/api/agents/status")
async def agents_status(request: Request) -> dict[str, Any]:
    """Health/status of this user's Scanner/Verifier/Executor/Tracker agents."""
    zerodha_id = get_session_zerodha_id(request)
    status = mcp_driver.get_agent_status(zerodha_id)
    return status or {"error": "No active agent for this session"}


@app.get("/api/agents/log")
async def agents_log(request: Request, cursor: str = "", agent: str = "", limit: int = 400) -> dict[str, Any]:
    """Incremental agent log for the console.

    ``cursor`` is ``scanner:120,executor:88,...`` - the last seq seen per agent;
    only newer entries are returned, plus the new cursor.
    """
    zerodha_id = get_session_zerodha_id(request)
    sup = mcp_driver.get_agent(zerodha_id)
    if sup is None:
        return {"error": "No active agent for this session", "entries": [], "cursor": cursor}
    seen: dict[str, int] = {}
    for part in cursor.split(","):
        name, _, seq = part.partition(":")
        if name and seq.isdigit():
            seen[name] = int(seq)
    limit = max(1, min(limit, 2000))
    names = [agent] if agent in sup.agents else list(sup.agents)
    entries: list[dict[str, Any]] = []
    for name in names:
        entries.extend(sup.agents[name].activity.log_since(seen.get(name, 0), limit))
    entries.sort(key=lambda e: (e["ts"], e["agent"], e["seq"]))
    entries = entries[-limit:]
    for e in entries:
        seen[e["agent"]] = max(seen.get(e["agent"], 0), e["seq"])
    for name in names:   # agents with nothing new keep their position
        seen.setdefault(name, sup.agents[name].activity.snapshot(limit=0)["last_seq"])
    return {"entries": entries, "cursor": ",".join(f"{k}:{v}" for k, v in seen.items())}


@app.get("/api/agents/lifecycle")
async def agents_lifecycle(request: Request, limit: int = 50) -> dict[str, Any]:
    """Recent signals with their life-cycle stages (signal -> checks -> order -> fill ->
    protection -> exit), newest first. Live updates arrive on /ws/live as
    ``{"type": "lifecycle", "data": <event>}``."""
    svc = get_user_service(request)
    return {"signals": svc.get_signal_lifecycles(max(1, min(limit, 300)))}


@app.get("/api/agents/log/download")
async def agents_log_download(request: Request, agent: str, day: str = "") -> Any:
    """Download one agent's JSONL log for a day (default today)."""
    from datetime import date as _date
    from fastapi.responses import FileResponse

    zerodha_id = get_session_zerodha_id(request)
    sup = mcp_driver.get_agent(zerodha_id)
    if sup is None or agent not in sup.agents:
        raise HTTPException(404, "Unknown agent")
    try:
        when = _date.fromisoformat(day) if day else _date.today()
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")
    path = sup.agents[agent].activity.log_path(when)
    if not path or not os.path.exists(path):
        raise HTTPException(404, f"No {agent} log for {when.isoformat()}")
    return FileResponse(path, media_type="application/x-ndjson", filename=os.path.basename(path))


@app.get("/api/research/best-stocks")
async def research_best_stocks(request: Request, top_n: int = 5) -> dict[str, Any]:
    """Top analytical stock picks via the supervised stock-research MCP agent."""
    return await mcp_driver.call_research_tool("get_best_stocks", top_n=top_n, enrich=True)


@app.get("/api/research/news")
async def research_news(request: Request, symbol: str, limit: int = 15) -> dict[str, Any]:
    """Latest news for an NSE symbol via the supervised stock-research MCP agent."""
    return await mcp_driver.call_research_tool("fetch_stock_news", symbol=symbol.upper().strip(), limit=limit)


@app.get("/api/research/stock/{symbol}")
async def research_stock(request: Request, symbol: str) -> dict[str, Any]:
    """Full research report for a single NSE symbol via the supervised stock-research MCP agent."""
    return await mcp_driver.call_research_tool("get_stock_research", symbol=symbol.upper().strip())


@app.get("/api/analysis/sectors")
async def analysis_sectors(request: Request, universe: Optional[str] = None) -> dict[str, Any]:
    """Sector-level analysis from cached scan data, for ``?universe=`` if given."""
    return get_user_service(request).get_sector_analysis(universe)


# ─── Paper Trading ─────────────────────────────────────────────

@app.post("/api/paper-trade/run")
async def run_paper_trade(request: Request) -> dict[str, Any]:
    """Run paper trade on historical data with a strategy.

    Symbol↔token binding: If tradingsymbol is provided, the server
    resolves the instrument_token from Zerodha's instrument list.
    Any client-supplied token is IGNORED to prevent mismatches.
    """
    body = await request.json()
    strategy_name = body.get("strategy", "")
    if not strategy_name:
        raise HTTPException(400, "strategy is required")

    tradingsymbol = (body.get("tradingsymbol") or "").strip().upper()
    exchange = body.get("exchange", "NSE")
    if not tradingsymbol:
        raise HTTPException(400, "tradingsymbol is required")

    svc = get_user_service(request)

    # ── Authoritative server-side token resolution ──────────────
    instrument_token = await svc.resolve_instrument_token(tradingsymbol, exchange)
    if not instrument_token:
        raise HTTPException(
            404,
            f"Instrument not found: {exchange}:{tradingsymbol}. "
            "Check symbol spelling and exchange.",
        )

    try:
        result = await svc.run_paper_trade(
            strategy_name=strategy_name,
            instrument_token=instrument_token,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            days=body.get("days", 60),
            interval=body.get("interval", "5minute"),
            capital=body.get("capital", 100000),
            commission_pct=body.get("commission_pct", 0.03),
            slippage_pct=body.get("slippage_pct", 0.05),
            strategy_params=body.get("strategy_params"),
        )
        return result
    except Exception as e:
        logger.error("paper_trade_error", error=str(e))
        raise HTTPException(500, _public_error(e))


@app.post("/api/paper-trade/sample")
async def run_paper_trade_sample(request: Request) -> dict[str, Any]:
    """Run paper trade with synthetic sample data (no auth to broker needed)."""
    body = await request.json()
    strategy_name = body.get("strategy", "")
    if not strategy_name:
        raise HTTPException(400, "strategy is required")
    svc = get_user_service(request)
    try:
        return await svc.run_paper_trade_with_sample_data(
            strategy_name=strategy_name,
            tradingsymbol=body.get("tradingsymbol", "SAMPLE"),
            bars=body.get("bars", 500),
            capital=body.get("capital", 100000),
            strategy_params=body.get("strategy_params"),
        )
    except Exception as e:
        logger.error("paper_trade_sample_error", error=str(e))
        raise HTTPException(500, _public_error(e))


@app.get("/api/paper-trade/results")
async def paper_trade_results(request: Request) -> dict[str, Any]:
    return get_user_service(request).get_paper_results()


# ─── Historical Data Import & Market Replay ───────────────────

@app.post("/api/data/import-historical")
async def import_historical_data(request: Request) -> dict[str, Any]:
    """Import historical candles from Zerodha into the local DB cache.

    Incremental: only fetches the gap since the last cached bar for this
    token+interval, so repeated clicks don't re-download the full history.
    """
    body = await request.json()
    tradingsymbol = (body.get("tradingsymbol") or "").strip().upper()
    exchange = body.get("exchange", "NSE")
    if not tradingsymbol:
        raise HTTPException(400, "tradingsymbol is required")

    svc = get_user_service(request)
    instrument_token = await svc.resolve_instrument_token(tradingsymbol, exchange)
    if not instrument_token:
        raise HTTPException(404, f"Instrument not found: {exchange}:{tradingsymbol}")

    try:
        return await svc.import_historical_data(
            instrument_token=instrument_token,
            tradingsymbol=tradingsymbol,
            interval=body.get("interval", "day"),
            days=int(body.get("days", 365)),
            from_date=body.get("from_date"),
            to_date=body.get("to_date"),
        )
    except Exception as e:
        logger.error("import_historical_data_error", error=str(e))
        raise HTTPException(500, _public_error(e))


@app.post("/api/replay/start")
async def start_market_replay(request: Request) -> dict[str, Any]:
    """Start a market replay: streams cached historical candles bar-by-bar
    over the live websocket while driving a strategy + paper-trade engine."""
    body = await request.json()
    strategy_name = body.get("strategy", "")
    if not strategy_name:
        raise HTTPException(400, "strategy is required")

    tradingsymbol = (body.get("tradingsymbol") or "").strip().upper()
    exchange = body.get("exchange", "NSE")
    if not tradingsymbol:
        raise HTTPException(400, "tradingsymbol is required")

    svc = get_user_service(request)
    instrument_token = await svc.resolve_instrument_token(tradingsymbol, exchange)
    if not instrument_token:
        raise HTTPException(404, f"Instrument not found: {exchange}:{tradingsymbol}")

    try:
        return await svc.start_replay(
            strategy_name=strategy_name,
            instrument_token=instrument_token,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            interval=body.get("interval", "5minute"),
            days=int(body.get("days", 30)),
            capital=float(body.get("capital", 100000)),
            speed=float(body.get("speed", 10)),
            strategy_params=body.get("strategy_params"),
        )
    except Exception as e:
        logger.error("replay_start_error", error=str(e))
        raise HTTPException(500, _public_error(e))


@app.post("/api/replay/stop")
async def stop_market_replay(request: Request) -> dict[str, Any]:
    return get_user_service(request).stop_replay()


@app.get("/api/replay/status")
async def market_replay_status(request: Request) -> dict[str, Any]:
    return get_user_service(request).get_replay_status()


# ─── Custom Strategy Builder ──────────────────────────────────

@app.get("/api/strategy-builder/indicators")
async def builder_indicators(request: Request) -> list[dict[str, Any]]:
    return get_user_service(request).get_builder_indicators()


@app.get("/api/strategy-builder/list")
async def list_custom_strategies(request: Request) -> list[dict[str, Any]]:
    return get_user_service(request).list_custom_strategies()


@app.get("/api/strategy-builder/get/{name}")
async def get_custom_strategy(request: Request, name: str) -> dict[str, Any]:
    result = get_user_service(request).get_custom_strategy(name)
    if result is None:
        raise HTTPException(404, f"Strategy '{name}' not found")
    return result


@app.post("/api/strategy-builder/save")
async def save_custom_strategy(request: Request) -> dict[str, Any]:
    body = await request.json()
    return get_user_service(request).save_custom_strategy(body)


@app.delete("/api/strategy-builder/{name}")
async def delete_custom_strategy(request: Request, name: str) -> dict[str, Any]:
    return get_user_service(request).delete_custom_strategy(name)


@app.post("/api/strategy-builder/test")
async def test_custom_strategy(request: Request) -> dict[str, Any]:
    """Save a custom strategy and immediately paper-trade it with sample data."""
    body = await request.json()
    svc = get_user_service(request)
    config = body.get("config", body)
    name = config.get("name", "")
    if not name:
        raise HTTPException(400, "Strategy name is required")

    # Save it first
    save_result = svc.save_custom_strategy(config)
    if "error" in save_result:
        return save_result

    # Paper trade with sample data
    try:
        result = await svc.run_paper_trade_with_sample_data(
            strategy_name=name,
            bars=body.get("bars", 500),
            capital=body.get("capital", 100000),
        )
        result["strategy_saved"] = True
        return result
    except Exception as e:
        logger.error("strategy_test_error", error=str(e))
        raise HTTPException(500, _public_error(e))


# ────────────────────────────────────────────────────────────────
# F&O Custom Strategy Builder APIs
# ────────────────────────────────────────────────────────────────

@app.get("/api/fno-builder/templates")
async def get_fno_builder_templates(request: Request) -> dict[str, Any]:
    """Get pre-built F&O strategy templates."""
    svc = get_user_service(request)
    return {"templates": svc.get_fno_builder_templates()}


@app.get("/api/fno-builder/list")
async def list_fno_custom_strategies(request: Request) -> list[dict[str, Any]]:
    """List saved custom F&O strategies."""
    svc = get_user_service(request)
    return svc.list_fno_custom_strategies()


@app.get("/api/fno-builder/get/{name}")
async def get_fno_custom_strategy(request: Request, name: str) -> dict[str, Any]:
    """Get a specific custom F&O strategy."""
    svc = get_user_service(request)
    result = svc.get_fno_custom_strategy(name)
    if not result:
        raise HTTPException(404, f"F&O strategy '{name}' not found")
    return result


@app.post("/api/fno-builder/save")
async def save_fno_custom_strategy(request: Request) -> dict[str, Any]:
    """Save a custom F&O strategy."""
    body = await request.json()
    svc = get_user_service(request)
    return svc.save_fno_custom_strategy(body)


@app.delete("/api/fno-builder/{name}")
async def delete_fno_custom_strategy(request: Request, name: str) -> dict[str, Any]:
    """Delete a custom F&O strategy."""
    svc = get_user_service(request)
    return svc.delete_fno_custom_strategy(name)


@app.post("/api/fno-builder/payoff")
async def compute_fno_payoff(request: Request) -> dict[str, Any]:
    """Compute payoff diagram for a custom F&O strategy config."""
    body = await request.json()
    svc = get_user_service(request)
    spot_price = float(body.get("spot_price", 25000))
    return svc.compute_fno_payoff(body.get("config", body), spot_price)


@app.post("/api/fno-builder/test")
async def test_fno_custom_strategy(request: Request) -> dict[str, Any]:
    """Save and run a quick backtest on a custom F&O strategy."""
    body = await request.json()
    svc = get_user_service(request)
    name = body.get("name", "custom_fno")

    # If legs are present, this is a full save+test from the builder
    if body.get("legs"):
        save_result = svc.save_fno_custom_strategy(body)
        if "error" in save_result:
            return save_result
    else:
        # Just a test of an already-saved strategy (from backtest/paper trade dropdowns)
        existing = svc.get_fno_custom_strategy(name)
        if not existing:
            return {"error": f"Custom F&O strategy '{name}' not found. Build it in the Strategy Builder first."}

    try:
        result = await svc.run_fno_backtest_sample(
            strategy_name=name,
            underlying=body.get("underlying", "NIFTY"),
            bars=int(body.get("bars", 500)),
            initial_capital=float(body.get("capital", 500000)),
        )
        result["strategy_saved"] = True
        return result
    except Exception as e:
        logger.error("fno_strategy_test_error", error=str(e))
        return {"error": _public_error(e), "strategy_saved": True}


@app.get("/api/analysis-strategies")
async def get_analysis_strategies(request: Request) -> dict[str, Any]:
    """Get list of individual analysis strategies for dropdowns."""
    svc = get_user_service(request)
    return {"strategies": svc.get_analysis_strategy_list()}


# ────────────────────────────────────────────────────────────────
# Strategy Health Report APIs
# ────────────────────────────────────────────────────────────────

@app.get("/api/strategy/health-report")
async def get_health_report(request: Request) -> dict[str, Any]:
    """Get the last computed strategy health report."""
    svc = get_user_service(request)
    return svc.get_last_health_report()


@app.post("/api/strategy/health-report")
async def compute_health_report_api(request: Request) -> dict[str, Any]:
    """Compute health report for specific backtest/paper-trade results.
    
    Body (optional):
        strategy_type: "equity" / "fno" / "intraday"
        strategy_name: override strategy name
    """
    svc = get_user_service(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return svc.compute_strategy_health(
        strategy_type=body.get("strategy_type", ""),
        strategy_name=body.get("strategy_name", ""),
    )


# ────────────────────────────────────────────────────────────────
# Historical Chart Data APIs
# ────────────────────────────────────────────────────────────────

@app.get("/api/chart/historical")
async def get_historical_chart(request: Request) -> dict[str, Any]:
    """Fetch historical OHLCV data for charting."""
    svc = get_user_service(request)
    params = request.query_params
    token = int(params.get("token", "0"))
    interval = params.get("interval", "day")
    days = int(params.get("days", "365"))
    from_date = params.get("from_date")
    to_date = params.get("to_date")
    indicators = params.get("include_indicators", params.get("indicators", "false")).lower() == "true"
    if token == 0:
        raise HTTPException(400, "instrument token required")
    return await svc.get_historical_chart(token, interval, days, from_date, to_date, indicators)


@app.get("/api/chart/historical/{symbol}")
async def get_historical_chart_by_symbol(symbol: str, request: Request) -> dict[str, Any]:
    """Fetch chart data by symbol name (auto-resolves token)."""
    svc = get_user_service(request)
    params = request.query_params
    exchange = params.get("exchange", "NSE")
    interval = params.get("interval", "day")
    days = int(params.get("days", "365"))
    from_date = params.get("from_date")
    to_date = params.get("to_date")
    indicators = params.get("include_indicators", params.get("indicators", "false")).lower() == "true"
    token = await svc.resolve_instrument_token(symbol, exchange)
    if not token:
        raise HTTPException(404, f"Instrument not found: {exchange}:{symbol}")
    return await svc.get_historical_chart(token, interval, days, from_date, to_date, indicators)


# ────────────────────────────────────────────────────────────────
# Live Market Quotes & Data APIs
# ────────────────────────────────────────────────────────────────

@app.get("/api/market/quote")
async def get_market_quote(request: Request) -> dict[str, Any]:
    """Full market quote. ?instruments=NSE:RELIANCE,NSE:TCS"""
    svc = get_user_service(request)
    raw = request.query_params.get("instruments", "")
    instruments = [i.strip() for i in raw.split(",") if i.strip()]
    if not instruments:
        raise HTTPException(400, "instruments parameter required (comma-separated)")
    return await svc.get_market_quote(instruments)


@app.get("/api/market/ltp")
async def get_market_ltp(request: Request) -> dict[str, Any]:
    """Last traded price. ?instruments=NSE:RELIANCE,NSE:TCS"""
    svc = get_user_service(request)
    raw = request.query_params.get("instruments", "")
    instruments = [i.strip() for i in raw.split(",") if i.strip()]
    if not instruments:
        raise HTTPException(400, "instruments parameter required")
    return await svc.get_market_ltp(instruments)


@app.get("/api/market/ohlc")
async def get_market_ohlc(request: Request) -> dict[str, Any]:
    """OHLC data. ?instruments=NSE:RELIANCE,NSE:TCS"""
    svc = get_user_service(request)
    raw = request.query_params.get("instruments", "")
    instruments = [i.strip() for i in raw.split(",") if i.strip()]
    if not instruments:
        raise HTTPException(400, "instruments parameter required")
    return await svc.get_market_ohlc(instruments)


@app.get("/api/market/overview")
async def get_market_overview(request: Request) -> dict[str, Any]:
    """Market overview for key indices/stocks."""
    svc = get_user_service(request)
    raw = request.query_params.get("symbols", "")
    symbols = [s.strip() for s in raw.split(",") if s.strip()] or None
    return await svc.get_market_overview(symbols)


@app.get("/api/market/nifty-contribution")
async def get_nifty_contribution(request: Request) -> dict[str, Any]:
    """Point contribution of each Nifty 50 constituent to the index move.
    ``?demo=1`` returns simulated data for previewing the view."""
    if request.query_params.get("demo") == "1":
        result = compute_nifty_contribution(_nifty_demo.quotes())
        result["demo"] = True
        return result
    svc = get_user_service(request)
    if not svc.is_authenticated:
        return {"error": "Trading account not linked"}
    return await svc.get_nifty_contribution()


@app.get("/api/market/fno-flow")
async def get_fno_flow(request: Request) -> Any:
    """Market flow for the F&O + NIFTY 500 universe: per-stock price, money flow,
    futures OI build-up, levels, bias/score, trade plans, best longs/shorts and
    sector flow per universe. ``?demo=1`` returns simulated data."""
    if request.query_params.get("demo") == "1":
        from datetime import datetime as _dt
        sim = _get_fno_demo()
        sim.step()
        now = _dt.now()
        result = compute_fno_flow(
            sim.universe, sim.quotes(), sim.baselines, now, _fno_demo_history.changes_ago(15, now),
        )
        _fno_demo_history.record(result, now)
        return {**result, "demo": True, "baseline_loading": False}
    svc = get_user_service(request)
    if not svc.is_authenticated:
        return {"error": "Trading account not linked"}
    # the same snapshot object for ~2 s (and for every viewer): serialise it once
    return shared_json(request, await svc.get_fno_flow())


_demo_day_trade: Optional[DayTradeTracker] = None


async def _demo_day_trade_snapshot(symbol: str | None = None, action: str = "add") -> dict[str, Any]:
    """Day-trade tracking over the demo simulator's synthetic 5-minute sessions."""
    from datetime import datetime as _dt

    global _demo_day_trade
    sim = _get_fno_demo()
    now = _dt.now()
    if _demo_day_trade is None:
        async def fetch(sym: str) -> list[dict[str, Any]]:
            return sim.intraday_bars(sym, _dt.now())
        _demo_day_trade = DayTradeTracker(fetch)
    flow = compute_fno_flow(sim.universe, sim.quotes(), sim.baselines, now, _fno_demo_history.changes_ago(15, now))
    if symbol:
        if action == "remove":
            _demo_day_trade.remove(symbol)
        elif not any(r["symbol"] == symbol for r in flow["rows"]):
            return {"error": f"{symbol} is not in the F&O or NIFTY 500 universe"}
        else:
            _demo_day_trade.add(symbol, now, next(r for r in flow["rows"] if r["symbol"] == symbol))
    _demo_day_trade.update_selection(flow, now, any_time=True)     # simulated sessions: picks at any hour
    await _demo_day_trade.refresh(now)
    # mark open trades to the simulated session's last close
    closes = {}
    for sym in _demo_day_trade.symbols:
        revealed = completed_session_bars(sim.intraday_bars(sym, now), now)
        if revealed:
            closes[sym] = revealed[-1]["c"]
    flow["rows"] = [{**r, "ltp": closes.get(r["symbol"], r["ltp"])} for r in flow["rows"]]
    return {**_demo_day_trade.snapshot(flow, now), "demo": True}


@app.get("/api/market/day-trade")
async def get_day_trade(request: Request) -> Any:
    """5-minute day-trade setups for the tracked list (money-flow picks + manual
    symbols): bars, VWAP / EMA / opening range, signals with outcomes, open
    trades and the session feed. ``?demo=1`` uses simulated sessions."""
    if request.query_params.get("demo") == "1":
        return await _demo_day_trade_snapshot()
    svc = get_user_service(request)
    if not svc.is_authenticated:
        return {"error": "Trading account not linked"}
    return shared_json(request, await svc.get_day_trade())


@app.get("/api/market/setup-rules")
async def get_setup_rules() -> dict[str, Any]:
    """The boards' setup rules in words (refused to the public demo: src/api/demo_scope.py)."""
    from src.api.setup_rules import setup_rules

    return setup_rules()


@app.get("/api/market/day-trade/replay")
async def replay_day_trade(request: Request, symbols: str = "", days: int = 30) -> dict[str, Any]:
    """Replay the 5-minute setups over past sessions: ``?symbols=INFY,TCS&days=30``
    (up to 10 symbols, 5-90 days). Results per setup / side / symbol, net of costs."""
    names = [s for s in symbols.split(",") if s.strip()]
    if not names or not all(re.fullmatch(r"[A-Za-z0-9&.-]{1,32}", n.strip()) for n in names):
        raise HTTPException(400, "symbols required, e.g. ?symbols=INFY,TCS")
    if request.query_params.get("demo") == "1":
        return {"error": "Replay needs real historical 5-minute data - turn DEMO off and link your trading account."}
    svc = get_user_service(request)
    if not svc.is_authenticated:
        return {"error": "Trading account not linked"}
    return await svc.replay_day_trade_setups(names, days)


@app.get("/api/oi/bias-history")
async def oi_bias_history(request: Request, underlying: str = "NIFTY", days: int = 1) -> dict[str, Any]:
    """Recorded per-minute options-OI bias (and its inputs) for back-testing direction filters."""
    return get_user_service(request).get_oi_bias_history(underlying, days)


@app.get("/api/market/day-trade/autotrade")
async def get_day_trade_autotrade(request: Request) -> dict[str, Any]:
    """Board auto-trade settings (opt-in; sends board signals through the live-trading path)."""
    return get_user_service(request).get_board_autotrade()


@app.post("/api/market/day-trade/autotrade")
async def set_day_trade_autotrade(request: Request) -> dict[str, Any]:
    """Update board auto-trade. Body: any of {enabled, risk_per_trade, max_order_value,
    max_orders_per_day, sides, setups, require_preflight}."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object expected")
    svc = get_user_service(request)
    if body.get("enabled") is True:
        svc.require_access(AGENTIC, "Auto-trading the Day Trade board")
    # Switching auto-trade off is always allowed; anything else can send orders.
    if body != {"enabled": False}:
        _require_step_up(request, svc)
    return svc.update_board_autotrade(body)


@app.post("/api/market/day-trade/track")
async def track_day_trade(request: Request) -> dict[str, Any]:
    """Add or remove a symbol from day-trade tracking. Body: {symbol, action: add|remove}."""
    body = await request.json()
    symbol = str(body.get("symbol") or "").upper().strip()
    action = body.get("action", "add")
    if not re.fullmatch(r"[A-Z0-9&.-]{1,32}", symbol) or action not in ("add", "remove"):
        raise HTTPException(400, "symbol and action (add|remove) required")
    if request.query_params.get("demo") == "1":
        result = await _demo_day_trade_snapshot(symbol, action)
        return {"error": result["error"]} if result.get("error") else {"success": True}
    svc = get_user_service(request)
    if not svc.is_authenticated:
        return {"error": "Trading account not linked"}
    return await svc.track_day_trade_symbol(symbol, action)


@app.get("/api/market/swing")
async def get_swing(request: Request) -> Any:
    """Swing board: 1-hour setups with daily context on the scanner's picks (plus
    symbols you track), signals with outcomes, hourly and daily bars.
    ``?demo=1`` uses the simulated market."""
    from src.api.service import _demo_flow_source

    if request.query_params.get("demo") == "1":
        return shared_json(request, await _demo_flow_source().get_swing())   # marked demo by the service
    svc = get_user_service(request)
    if not svc.is_authenticated:
        return {"error": "Trading account not linked"}
    return shared_json(request, await svc.get_swing())


@app.post("/api/market/swing/track")
async def track_swing(request: Request) -> dict[str, Any]:
    """Add or remove a symbol from the swing board. Body: {symbol, action: add|remove}."""
    from src.api.service import _demo_flow_source

    body = await request.json()
    symbol = str(body.get("symbol") or "").upper().strip()
    action = body.get("action", "add")
    if not re.fullmatch(r"[A-Z0-9&.-]{1,32}", symbol) or action not in ("add", "remove"):
        raise HTTPException(400, "symbol and action (add|remove) required")
    if request.query_params.get("demo") == "1":
        return await _demo_flow_source().track_swing_symbol(symbol, action)
    svc = get_user_service(request)
    if not svc.is_authenticated:
        return {"error": "Trading account not linked"}
    return await svc.track_swing_symbol(symbol, action)


@app.get("/api/market/fno-flow/history")
async def get_fno_flow_history(request: Request, minutes: int = 60) -> dict[str, Any]:
    """Minute snapshots ({symbol: [chg%, oi%, score]}) for the time rail."""
    minutes = max(1, min(minutes, 375))
    if request.query_params.get("demo") == "1":
        from datetime import datetime as _dt
        _get_fno_demo()
        return {"points": _fno_demo_history.since(minutes, _dt.now())}
    return {"points": get_user_service(request).get_fno_flow_history(minutes)}


@app.get("/api/market/ticks")
async def get_live_ticks_snapshot(request: Request) -> dict[str, Any]:
    """Get current tick cache from live WebSocket."""
    svc = get_user_service(request)
    return {"ticks": svc.get_live_ticks()}


@app.get("/api/market/resolve-token")
async def resolve_instrument_token(request: Request) -> dict[str, Any]:
    """Resolve a tradingsymbol to its instrument_token.

    Usage: /api/market/resolve-token?symbol=RELIANCE&exchange=NSE
    Returns: {"symbol": "RELIANCE", "exchange": "NSE", "instrument_token": 738561}
    """
    symbol = (request.query_params.get("symbol") or "").strip().upper()
    exchange = request.query_params.get("exchange", "NSE").strip().upper()
    if not symbol:
        raise HTTPException(400, "symbol query param is required")
    svc = get_user_service(request)
    token = await svc.resolve_instrument_token(symbol, exchange)
    if not token:
        raise HTTPException(404, f"Instrument not found: {exchange}:{symbol}")
    return {"symbol": symbol, "exchange": exchange, "instrument_token": token}


# ────────────────────────────────────────────────────────────────
# Portfolio Reports & Investing APIs
# ────────────────────────────────────────────────────────────────

@app.get("/api/reports/holdings")
async def get_holdings_report(request: Request) -> dict[str, Any]:
    """Comprehensive holdings report with P&L analysis."""
    svc = get_user_service(request)
    try:
        return await svc.get_holdings_report()
    except Exception as e:
        logger.error("holdings_report_route_error", error=str(e))
        return {"holdings": [], "summary": {}, "error": _public_error(e)}


@app.get("/api/reports/positions")
async def get_positions_report(request: Request) -> dict[str, Any]:
    """Positions report with realized/unrealized P&L."""
    svc = get_user_service(request)
    try:
        return await svc.get_positions_report()
    except Exception as e:
        logger.error("positions_report_route_error", error=str(e))
        return {"positions": [], "summary": {}, "error": _public_error(e)}


@app.get("/api/reports/trades")
async def get_trades_report(request: Request) -> dict[str, Any]:
    """Today's trade execution report."""
    svc = get_user_service(request)
    try:
        return await svc.get_trades_report()
    except Exception as e:
        logger.error("trades_report_route_error", error=str(e))
        return {"trades": [], "summary": {}, "error": _public_error(e)}


@app.get("/api/reports/orders")
async def get_orders_report(request: Request) -> dict[str, Any]:
    """Orders report with status breakdown."""
    svc = get_user_service(request)
    try:
        return await svc.get_orders_report()
    except Exception as e:
        logger.error("orders_report_route_error", error=str(e))
        return {"orders": [], "summary": {}, "error": _public_error(e)}


@app.get("/api/reports/pnl")
async def get_pnl_report(request: Request) -> dict[str, Any]:
    """Combined P&L report (positions + holdings)."""
    svc = get_user_service(request)
    try:
        return await svc.get_pnl_report()
    except Exception as e:
        logger.error("pnl_report_route_error", error=str(e))
        return {"error": _public_error(e)}


@app.get("/api/reports/margins")
async def get_margins_report(request: Request) -> dict[str, Any]:
    """Detailed margin utilization report."""
    svc = get_user_service(request)
    try:
        return await svc.get_margins_report()
    except Exception as e:
        logger.error("margins_report_route_error", error=str(e))
        return {"margins": {}, "error": _public_error(e)}


# ────────────────────────────────────────────────────────────────
# Advanced Backtesting APIs
# ────────────────────────────────────────────────────────────────

@app.post("/api/backtest/run")
async def run_backtest(request: Request) -> dict[str, Any]:
    """Run advanced backtest on live Zerodha historical data.

    Symbol↔token binding: tradingsymbol is resolved server-side
    to the correct instrument_token. Client-sent token is IGNORED.
    """
    body = await request.json()
    svc = get_user_service(request)

    tradingsymbol = (body.get("tradingsymbol") or "").strip().upper()
    exchange = body.get("exchange", "NSE")
    if not tradingsymbol:
        raise HTTPException(400, "tradingsymbol is required for live backtest")

    # ── Authoritative server-side token resolution ──────────────
    instrument_token = await svc.resolve_instrument_token(tradingsymbol, exchange)
    if not instrument_token:
        raise HTTPException(
            404,
            f"Instrument not found: {exchange}:{tradingsymbol}. "
            "Check symbol spelling and exchange.",
        )

    try:
        result = await svc.run_backtest(
            strategy_name=body.get("strategy", "ema_crossover"),
            instrument_token=instrument_token,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            interval=body.get("interval", "day"),
            days=body.get("days", 365),
            from_date=body.get("from_date"),
            to_date=body.get("to_date"),
            initial_capital=body.get("capital", 100000),
            position_sizing=body.get("position_sizing", "fixed"),
            risk_per_trade=body.get("risk_per_trade", 0.02),
            capital_fraction=body.get("capital_fraction", 0.10),
            slippage_pct=body.get("slippage_pct", 0.05),
            use_indian_costs=body.get("use_indian_costs", True),
            is_intraday=body.get("is_intraday", True),
            strategy_params=body.get("strategy_params"),
        )
        return result
    except Exception as e:
        logger.error("backtest_error", error=str(e))
        raise HTTPException(500, _public_error(e))


@app.post("/api/backtest/sample")
async def run_backtest_sample(request: Request) -> dict[str, Any]:
    """Run backtest on synthetic sample data."""
    body = await request.json()
    svc = get_user_service(request)

    try:
        result = await svc.run_backtest_sample(
            strategy_name=body.get("strategy", "ema_crossover"),
            bars=body.get("bars", 500),
            initial_capital=body.get("capital", 100000),
            position_sizing=body.get("position_sizing", "fixed"),
            slippage_pct=body.get("slippage_pct", 0.05),
            use_indian_costs=body.get("use_indian_costs", True),
            is_intraday=body.get("is_intraday", False),
            risk_per_trade=body.get("risk_per_trade", 0.02),
            capital_fraction=body.get("capital_fraction", 0.10),
            strategy_params=body.get("strategy_params"),
        )
        return result
    except Exception as e:
        logger.error("backtest_sample_error", error=str(e))
        raise HTTPException(500, _public_error(e))


@app.get("/api/backtest/results")
async def get_backtest_results(request: Request) -> dict[str, Any]:
    """Get cached backtest results."""
    svc = get_user_service(request)
    return svc.get_backtest_results()


# ────────────────────────────────────────────────────────
# F&O Derivatives Backtest & Paper Trade
# ────────────────────────────────────────────────────────

@app.get("/api/fno/strategies")
async def get_fno_strategies(request: Request) -> dict[str, Any]:
    """Get available F&O option strategies."""
    svc = get_user_service(request)
    return {"strategies": svc.get_fno_strategies()}


@app.post("/api/data/import-fno-historical")
async def import_fno_historical_data(request: Request) -> dict[str, Any]:
    """Import historical underlying candles for an F&O index into the local DB cache.

    Incremental: only fetches the gap since the last cached bar (same as
    /api/data/import-historical, but resolves the token from the well-known
    index map since Zerodha's index tradingsymbols don't match the plain
    underlying name used by the F&O tabs, e.g. "NIFTY" -> "NIFTY 50").
    """
    body = await request.json()
    underlying = (body.get("underlying") or "NIFTY").strip().upper()
    svc = get_user_service(request)
    instrument_token = svc.resolve_underlying_token(underlying, int(body.get("instrument_token", 0)))
    if not instrument_token:
        raise HTTPException(400, f"Unknown underlying: {underlying}")

    try:
        return await svc.import_historical_data(
            instrument_token=instrument_token,
            tradingsymbol=underlying,
            interval=body.get("interval", "day"),
            days=int(body.get("days", 365)),
            from_date=body.get("from_date"),
            to_date=body.get("to_date"),
        )
    except Exception as e:
        logger.error("import_fno_historical_data_error", error=str(e))
        raise HTTPException(500, _public_error(e))


@app.post("/api/fno-backtest/run")
async def run_fno_backtest(request: Request) -> dict[str, Any]:
    """Run F&O derivatives backtest on real underlying data."""
    body = await request.json()
    svc = get_user_service(request)
    return await svc.run_fno_backtest(
        strategy_name=body.get("strategy", "iron_condor"),
        underlying=body.get("underlying", "NIFTY"),
        instrument_token=int(body.get("instrument_token", 0)),
        interval=body.get("interval", "day"),
        days=int(body.get("days", 365)),
        from_date=body.get("from_date"),
        to_date=body.get("to_date"),
        initial_capital=float(body.get("capital", 500000)),
        max_positions=int(body.get("max_positions", 3)),
        profit_target_pct=float(body.get("profit_target_pct", 50)),
        stop_loss_pct=float(body.get("stop_loss_pct", 100)),
        entry_dte_min=int(body.get("entry_dte_min", 15)),
        entry_dte_max=int(body.get("entry_dte_max", 45)),
        delta_target=float(body.get("delta_target", 0.16)),
        slippage_model=body.get("slippage_model", "realistic"),
        use_regime_filter=body.get("use_regime_filter", True),
    )


@app.post("/api/fno-backtest/sample")
async def run_fno_backtest_sample(request: Request) -> dict[str, Any]:
    """Run F&O backtest on synthetic data (no auth required for data)."""
    body = await request.json()
    svc = get_user_service(request)
    return await svc.run_fno_backtest_sample(
        strategy_name=body.get("strategy", "iron_condor"),
        underlying=body.get("underlying", "NIFTY"),
        bars=int(body.get("bars", 500)),
        initial_capital=float(body.get("capital", 500000)),
        max_positions=int(body.get("max_positions", 3)),
        profit_target_pct=float(body.get("profit_target_pct", 50)),
        stop_loss_pct=float(body.get("stop_loss_pct", 100)),
        delta_target=float(body.get("delta_target", 0.16)),
    )


@app.post("/api/fno-paper-trade/run")
async def run_fno_paper_trade(request: Request) -> dict[str, Any]:
    """Run F&O paper trading simulation on real underlying data."""
    body = await request.json()
    svc = get_user_service(request)
    return await svc.run_fno_paper_trade(
        strategy_name=body.get("strategy", "iron_condor"),
        underlying=body.get("underlying", "NIFTY"),
        instrument_token=int(body.get("instrument_token", 0)),
        interval=body.get("interval", "day"),
        days=int(body.get("days", 60)),
        initial_capital=float(body.get("capital", 500000)),
        max_positions=int(body.get("max_positions", 3)),
        profit_target_pct=float(body.get("profit_target_pct", 50)),
        stop_loss_pct=float(body.get("stop_loss_pct", 100)),
        entry_dte_min=int(body.get("entry_dte_min", 15)),
        entry_dte_max=int(body.get("entry_dte_max", 45)),
        delta_target=float(body.get("delta_target", 0.16)),
        slippage_model=body.get("slippage_model", "realistic"),
    )


@app.post("/api/fno-paper-trade/sample")
async def run_fno_paper_trade_sample(request: Request) -> dict[str, Any]:
    """Run F&O paper trade on synthetic data (no auth required for data)."""
    body = await request.json()
    svc = get_user_service(request)
    return await svc.run_fno_paper_trade_sample(
        strategy_name=body.get("strategy", "iron_condor"),
        underlying=body.get("underlying", "NIFTY"),
        bars=int(body.get("bars", 500)),
        initial_capital=float(body.get("capital", 500000)),
        max_positions=int(body.get("max_positions", 3)),
        profit_target_pct=float(body.get("profit_target_pct", 50)),
        stop_loss_pct=float(body.get("stop_loss_pct", 100)),
        delta_target=float(body.get("delta_target", 0.16)),
    )


@app.post("/api/risk/kill-switch")
async def toggle_kill_switch(request: Request) -> dict[str, Any]:
    body = await request.json()
    activate = body.get("activate") if isinstance(body, dict) else None
    if not isinstance(activate, bool):
        raise HTTPException(400, "'activate' must be true or false")
    svc = get_user_service(request)
    if activate:
        svc._risk.activate_kill_switch("Manual activation via dashboard")
    else:
        _require_step_up(request, svc)
        try:
            svc._risk.deactivate_kill_switch()
        except KillSwitchError as e:
            raise HTTPException(409, e.message)
    return svc.get_risk_summary()


# ────────────────────────────────────────────────────────────
# 3-Layer Production Journal API
# ────────────────────────────────────────────────────────────

@app.get("/api/pro-journal/summary")
async def get_pro_journal_summary(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        p = request.query_params
        return svc.get_pro_journal_summary(
            source=p.get("source", ""), days=int(p.get("days", 0)),
            strategy=p.get("strategy", ""), instrument=p.get("instrument", ""),
            direction=p.get("direction", ""), trade_type=p.get("trade_type", ""))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_summary_error", error=str(e))
        return {"error": _public_error(e), "total_trades": 0}


@app.get("/api/pro-journal/analytics")
async def get_journal_analytics(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        p = request.query_params
        return svc.get_journal_analytics(
            strategy=p.get("strategy", ""), source=p.get("source", ""),
            days=int(p.get("days", 0)), instrument=p.get("instrument", ""),
            direction=p.get("direction", ""), trade_type=p.get("trade_type", ""))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_analytics_error", error=str(e))
        return {"error": _public_error(e), "total_trades": 0}


@app.get("/api/pro-journal/fno-analytics")
async def get_fno_journal_analytics(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        p = request.query_params
        return svc.get_fno_journal_analytics(
            strategy=p.get("strategy", ""), days=int(p.get("days", 0)),
            instrument=p.get("instrument", ""), direction=p.get("direction", ""),
            trade_type=p.get("trade_type", ""), source=p.get("source", ""))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_fno_analytics_error", error=str(e))
        return {"error": _public_error(e)}


@app.get("/api/pro-journal/entries")
async def get_journal_entries(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        params = request.query_params
        is_closed = None
        if "is_closed" in params:
            is_closed = params["is_closed"].lower() == "true"
        from_date = params.get("from_date", "")
        to_date = params.get("to_date", "")
        # Convert 'days' filter to from_date if not already specified
        days = int(params.get("days", 0))
        if days and not from_date:
            from datetime import datetime, timedelta
            from_date = (datetime.now() - timedelta(days=days)).isoformat()
        result = svc.get_journal_entries(
            strategy=params.get("strategy", ""),
            instrument=params.get("instrument", ""),
            trade_type=params.get("trade_type", ""),
            source=params.get("source", ""),
            direction=params.get("direction", ""),
            review_status=params.get("review_status", ""),
            is_closed=is_closed,
            from_date=from_date,
            to_date=to_date,
            limit=int(params.get("page_size", params.get("limit", 100))),
            offset=(int(params.get("page", 1)) - 1) * int(params.get("page_size", params.get("limit", 100))),
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_entries_error", error=str(e))
        return {"entries": [], "total": 0, "error": _public_error(e)}


@app.get("/api/pro-journal/entry/{entry_id}")
async def get_journal_entry(request: Request, entry_id: str) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        entry = svc.get_journal_entry(entry_id)
        if not entry:
            return {"error": "Entry not found"}
        return entry
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_entry_error", error=str(e))
        return {"error": _public_error(e)}


@app.post("/api/pro-journal/entry")
async def create_journal_entry(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        svc = get_user_service(request)
        entry_id = svc.record_journal_entry(body)
        return {"entry_id": entry_id, "status": "created"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_create_error", error=str(e))
        return {"error": _public_error(e)}


@app.put("/api/pro-journal/entry/{entry_id}")
async def update_journal_entry(request: Request, entry_id: str) -> dict[str, Any]:
    try:
        body = await request.json()
        svc = get_user_service(request)
        ok = svc.update_journal_entry(entry_id, body)
        return {"updated": ok}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_update_error", error=str(e))
        return {"error": _public_error(e)}


@app.get("/api/pro-journal/regime-matrix")
async def get_regime_matrix(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        p = request.query_params
        return {"matrix": svc.get_regime_matrix(
            strategy=p.get("strategy", ""), instrument=p.get("instrument", ""),
            source=p.get("source", ""), direction=p.get("direction", ""),
            trade_type=p.get("trade_type", ""), days=int(p.get("days", 0)))}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_regime_error", error=str(e))
        return {"matrix": []}


@app.get("/api/pro-journal/slippage-drift")
async def get_slippage_drift(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        p = request.query_params
        return svc.get_slippage_drift(
            days=int(p.get("days", 30)), strategy=p.get("strategy", ""),
            instrument=p.get("instrument", ""), source=p.get("source", ""),
            direction=p.get("direction", ""), trade_type=p.get("trade_type", ""))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_slippage_error", error=str(e))
        return {"error": _public_error(e)}


@app.get("/api/pro-journal/daily-pnl")
async def get_daily_pnl_history(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        p = request.query_params
        return {"daily_pnl": svc.get_daily_pnl_history(
            days=int(p.get("days", 30)), strategy=p.get("strategy", ""),
            instrument=p.get("instrument", ""), source=p.get("source", ""),
            direction=p.get("direction", ""), trade_type=p.get("trade_type", ""))}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_daily_pnl_error", error=str(e))
        return {"daily_pnl": []}


@app.get("/api/pro-journal/strategy-breakdown")
async def get_strategy_breakdown(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        p = request.query_params
        return {"strategies": svc.get_strategy_breakdown(
            source=p.get("source", ""), days=int(p.get("days", 0)),
            instrument=p.get("instrument", ""), direction=p.get("direction", ""),
            trade_type=p.get("trade_type", ""), strategy=p.get("strategy", ""))}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_strategy_error", error=str(e))
        return {"strategies": []}


@app.get("/api/pro-journal/portfolio-health")
async def get_portfolio_health(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        return svc.get_portfolio_health()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_portfolio_error", error=str(e))
        return {"error": _public_error(e)}


@app.get("/api/pro-journal/portfolio-health/history")
async def get_portfolio_health_history(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        days = int(request.query_params.get("days", 30))
        return svc.get_portfolio_health_history(days=days)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_health_history_error", error=str(e))
        return {"error": _public_error(e)}


@app.get("/api/pro-journal/equity-curve")
async def get_equity_curve(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        days = int(request.query_params.get("days", 365))
        return {"curve": svc.get_equity_curve(days=days)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_equity_error", error=str(e))
        return {"curve": []}


@app.get("/api/pro-journal/system-events")
async def get_system_events(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        event_type = request.query_params.get("event_type", "")
        limit = int(request.query_params.get("limit", 100))
        return {"events": svc.get_system_events(event_type=event_type, limit=limit)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_events_error", error=str(e))
        return {"events": []}


@app.get("/api/pro-journal/db-stats")
async def get_journal_db_stats(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        return svc.get_journal_db_stats()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_stats_error", error=str(e))
        return {"error": _public_error(e)}


@app.get("/api/pro-journal/filters")
async def get_journal_filters(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        return {
            "strategies": svc.get_journal_strategies(),
            "instruments": svc.get_journal_instruments(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_filters_error", error=str(e))
        return {"strategies": [], "instruments": []}


@app.get("/api/pro-journal/export")
async def export_journal(request: Request) -> dict[str, Any]:
    try:
        svc = get_user_service(request)
        return {"entries": svc.export_journal()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pro_journal_export_error", error=str(e))
        return {"entries": [], "error": _public_error(e)}


@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket) -> None:
    if not _same_origin(websocket):
        await websocket.close(code=4003)
        return
    try:
        svc = _service_for_session(websocket.cookies.get(SESSION_COOKIE, ""))
    except HTTPException:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    svc.register_ws_client(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        svc.unregister_ws_client(websocket)
    except Exception:
        svc.unregister_ws_client(websocket)


app.include_router(admin_router)
