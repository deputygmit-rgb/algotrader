"""Day Trade and Swing signals to a private Telegram channel: one-way, posting only.

How it is wired (``.env``):

* ``TELEGRAM_BOT_TOKEN`` - from @BotFather. Kept server-side only: never logged, never
  sent to a browser, never part of an error message.
* ``TELEGRAM_CHAT_ID``   - the numeric id of a **private** channel (``-100…``). The bot is
  an administrator of that channel with only "Post messages"; members can read, nobody
  can reply, and the bot reads nothing from Telegram (no webhook, no getUpdates).

Every message is sent with ``protect_content`` (no forwarding or saving) and without link
previews. Only the market-data account posts, so each signal goes out once; posted ids
are kept per day (``data/telegram/sent-<day>.json``) so a restart never repeats one.
Messages are queued and spaced (Telegram allows ~20 a minute in a channel).
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import time
from datetime import date
from typing import Any, Optional

import httpx

from src.utils.logger import get_logger

logger = get_logger(__name__)

API = "https://api.telegram.org"
MIN_GAP_S = 3.2                     # ~18 messages a minute, under the channel limit
TOP_N = 5
SENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "data", "telegram")
DISCLAIMER = ("Rule-based setup from VerquoAI's analytics - information only, not a recommendation "
              "or investment advice. Trading involves substantial risk.")


TOKEN_RE = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,60}")
CHAT_RE = re.compile(r"-?\d{5,20}|@[A-Za-z0-9_]{5,32}")
_reported: set[str] = set()


def _clean(name: str) -> str:
    return os.environ.get(name, "").strip().strip("\"'").strip()


def config_problem() -> str:
    """Why posting is off, in words (never the values): "" when the settings are usable."""
    token, chat = _clean("TELEGRAM_BOT_TOKEN"), _clean("TELEGRAM_CHAT_ID")
    if not token and not chat:
        return "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set"
    if not token:
        return "TELEGRAM_BOT_TOKEN is not set"
    if not TOKEN_RE.fullmatch(token):
        return "TELEGRAM_BOT_TOKEN is not a bot token (digits, a colon, ~35 characters - as @BotFather gives it)"
    if not chat:
        return "TELEGRAM_CHAT_ID is not set"
    if "t.me" in chat or chat.startswith("http"):
        return "TELEGRAM_CHAT_ID holds a link: it must be the channel's numeric id (-100...)"
    if not CHAT_RE.fullmatch(chat):
        return "TELEGRAM_CHAT_ID is not a channel id (-100... number)"
    return ""


def _config() -> tuple[str, str]:
    problem = config_problem()
    if problem:
        # once per problem: the settings are partly there but unusable (not when Telegram is simply off)
        if (_clean("TELEGRAM_BOT_TOKEN") or _clean("TELEGRAM_CHAT_ID")) and problem not in _reported:
            _reported.add(problem)
            logger.warning("telegram_not_configured", problem=problem)
        return "", ""
    return _clean("TELEGRAM_BOT_TOKEN"), _clean("TELEGRAM_CHAT_ID")


def enabled() -> bool:
    return all(_config())


def _num(v: Any, digits: int = 2) -> str:
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _pct(v: Any) -> str:
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def flow_leaders(flow: dict[str, Any], n: int = TOP_N) -> dict[str, list[dict[str, Any]]]:
    """Money flow's top longs and shorts in the F&O universe (the stocks with futures OI)."""
    rows = {r.get("symbol"): r for r in flow.get("rows") or []}
    picks = (flow.get("picks") or {}).get("fno") or {}
    out: dict[str, list[dict[str, Any]]] = {}
    for side in ("long", "short"):
        out[side] = [rows[s] for s in (picks.get(side) or []) if s in rows][:n]
    return out


def format_signal(board: str, s: dict[str, Any], flow: dict[str, Any]) -> str:
    """The signal, why it triggered, its plan, and money flow's top longs / shorts with OI."""
    e = html.escape
    timeframe = "5-min" if board == "day" else "1-hour"
    title = "DAY TRADE" if board == "day" else "SWING"
    lines = [
        f"<b>{title} · {timeframe} · {e(s.get('side', ''))} {e(str(s.get('symbol', '')))}</b>",
        f"Setup: {e(str(s.get('label') or s.get('setup') or ''))}",
        f"Why: {e(str(s.get('trigger') or ''))}",
    ]
    detail = [f"quality {s.get('quality', '-')}/100"]
    if s.get("vol_x") is not None:
        detail.append(f"volume {_num(s['vol_x'], 1)}× recent")
    if s.get("tod_x") is not None:
        detail.append(f"{_num(s['tod_x'], 1)}× normal for the hour")
    if s.get("market_bias"):
        adj = s.get("market_adj") or 0
        detail.append(f"NIFTY options-OI {e(str(s['market_bias']))} ({adj:+d})")
    lines.append(" · ".join(detail))
    if s.get("entry"):
        lines.append(f"Plan (est.): entry {_num(s.get('entry'))} · stop {_num(s.get('stop'))} · "
                     f"T1 {_num(s.get('target1'))} · T2 {_num(s.get('target2'))}")
    lines.append(f"Signal at {e(str(s.get('time') or ''))} IST (bar close)")

    leaders = flow_leaders(flow)
    for side, label in (("long", "Money flow - top longs"), ("short", "Money flow - top shorts")):
        rows = leaders.get(side) or []
        if not rows:
            continue
        lines.append("")
        lines.append(f"<b>{label}</b>")
        for r in rows:
            oi = f"OI {_pct(r.get('oi_change_pct'))}" + (f" {e(str(r['buildup']))}" if r.get("buildup") else "")
            lines.append(f"{e(str(r.get('symbol')))} {_pct(r.get('change_pct'))} · {oi} · flow {r.get('flow_score', '-')}")
    lines += ["", f"<i>{e(DISCLAIMER)}</i>"]
    return "\n".join(lines)[:4000]         # Telegram's limit is 4096 characters


class TelegramPublisher:
    """Queues and posts new signals; one per id, ever."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        self._task: Optional[asyncio.Task] = None
        self._day = ""
        self._sent: set[str] = set()
        self._last_post = 0.0

    # ── what was already posted today (survives restarts) ──
    def _path(self, day: str) -> str:
        return os.path.join(SENT_DIR, f"sent-{day}.json")

    def _load(self) -> None:
        day = date.today().isoformat()
        if day == self._day:
            return
        self._day = day
        try:
            with open(self._path(day), encoding="utf-8") as f:
                self._sent = set(json.load(f))
        except (OSError, ValueError):
            self._sent = set()

    def _save(self) -> None:
        try:
            os.makedirs(SENT_DIR, exist_ok=True)
            tmp = self._path(self._day) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sorted(self._sent), f)
            os.replace(tmp, self._path(self._day))
        except OSError as e:
            logger.warning("telegram_sent_save_failed", error=str(e))

    # ── public ──
    def publish_new(self, board: str, snap: dict[str, Any], flow: dict[str, Any]) -> int:
        """Queue each signal that is new on its bar (not replayed, not skipped). Returns how many."""
        if not enabled():
            return 0
        self._load()
        queued = 0
        for s in snap.get("feed") or []:
            if s.get("status") != "PENDING" or not s.get("is_latest_bar") or s.get("backfilled") or s.get("skipped"):
                continue
            key = f"{board}:{s.get('symbol')}:{s.get('id')}"
            if key in self._sent:
                continue
            self._sent.add(key)
            try:
                self._queue.put_nowait(format_signal(board, s, flow))
                queued += 1
            except asyncio.QueueFull:
                logger.warning("telegram_queue_full")
                break
        if queued:
            self._save()
            self._ensure_worker()
        return queued

    def _ensure_worker(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._worker(), name="telegram")

    async def _worker(self) -> None:
        while not self._queue.empty():
            text = await self._queue.get()
            wait = MIN_GAP_S - (time.monotonic() - self._last_post)
            if wait > 0:
                await asyncio.sleep(wait)
            await send(text)
            self._last_post = time.monotonic()


async def send(text: str) -> bool:
    """Post one message to the channel. The token is never logged or returned."""
    token, chat = _config()
    if not token:
        return False
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True, "protect_content": True}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{API}/bot{token}/sendMessage", json=payload)
        if r.status_code == 429:                       # flood control: wait as told, once
            retry = float((r.json().get("parameters") or {}).get("retry_after", 5))
            await asyncio.sleep(min(retry, 60))
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(f"{API}/bot{token}/sendMessage", json=payload)
        if r.status_code != 200:
            logger.warning("telegram_send_failed", status=r.status_code,
                           description=str((r.json() if r.content else {}).get("description", ""))[:120])
            return False
        return True
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("telegram_send_failed", error=type(e).__name__)   # no URL: it holds the token
        return False


publisher = TelegramPublisher()
