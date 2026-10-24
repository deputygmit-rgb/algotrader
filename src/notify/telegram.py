"""Day Trade and Swing signals to a private Telegram channel: one-way, posting only.

How it is wired (``.env``):

* ``TELEGRAM_BOT_TOKEN`` - from @BotFather. Kept server-side only: never logged, never
  sent to a browser, never part of an error message.
* ``TELEGRAM_CHAT_ID``   - the numeric id of a **private** channel (``-100…``). The bot is
  an administrator of that channel with only "Post messages"; members can read, nobody
  can reply, and the bot reads nothing from Telegram (no webhook, no getUpdates).

Every message is sent with ``protect_content`` (no forwarding or saving) and without link
previews. Only the market-data account posts, so each signal goes out once. Each posted
signal is then followed to its close - entry filled, T1, and the exit with its result - as
replies to its post. What was posted and how far each signal got is kept in
``data/telegram/posted.json`` so a restart never repeats a message.
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
    if chat.isdigit():
        # a positive id is one person's chat with the bot: posts would go to them, not a channel
        return "TELEGRAM_CHAT_ID is a person's chat, not the channel: the channel's id starts with -100"
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


# A posted signal is followed to its close: each stage it reaches is posted once, as a
# reply to the signal's own post, so the whole cycle reads as one thread.
STAGE = {"PENDING": 0, "ACTIVE": 1, "T1 HIT": 2, "TARGET 2": 3, "STOPPED": 3, "BREAKEVEN": 3, "TIME EXIT": 3}
KEEP_DAYS = 30                      # swing trades can stay open for days


def _r(v: Any) -> str:
    try:
        return f"{float(v):+.2f}R"
    except (TypeError, ValueError):
        return "-"


def format_update(board: str, s: dict[str, Any], was: str) -> str:
    """The stage a posted signal has reached (and any it passed since the last post)."""
    e = html.escape
    title = "DAY TRADE" if board == "day" else "SWING"
    status = s.get("status", "")
    head = f"<b>{title} · {e(str(s.get('side', '')))} {e(str(s.get('symbol', '')))} · "
    filled = (f"Entry filled {_num(s.get('entry'))}" + (f" at {e(str(s['entry_time']))}" if s.get("entry_time") else ""))
    lines: list[str] = []
    if status == "ACTIVE":
        lines = [head + "ENTRY FILLED</b>", filled,
                 f"Stop {_num(s.get('stop'))} · T1 {_num(s.get('target1'))} · T2 {_num(s.get('target2'))}"]
    elif status == "T1 HIT":
        lines = [head + "T1 HIT</b>"] + ([filled] if STAGE.get(was, 0) < 1 else []) + [
            f"Half booked at T1 {_num(s.get('target1'))}; stop moved to entry {_num(s.get('entry'))}. "
            f"Rest aims for T2 {_num(s.get('target2'))}"]
    else:
        passed_t1 = status in ("TARGET 2", "BREAKEVEN") or (status == "TIME EXIT" and STAGE.get(was, 0) >= 2)
        outcome = {
            "TARGET 2": f"Target 2 hit at {_num(s.get('target2'))}",
            "STOPPED": f"Stopped out at {_num(s.get('exit_price') or s.get('stop'))}",
            "BREAKEVEN": f"Rest stopped at entry {_num(s.get('entry'))} after T1",
            "TIME EXIT": (f"Closed at the time limit" + (f" at {_num(s['exit_price'])}" if s.get("exit_price") else "")
                          + (" (half was booked at T1)" if passed_t1 else "")),
        }.get(status, e(status.title()))
        lines = [head + f"CLOSED · {e(status)}</b>"]
        if STAGE.get(was, 0) < 1:
            lines.append(filled)
        if status in ("TARGET 2", "BREAKEVEN") and STAGE.get(was, 0) < 2:
            lines.append(f"T1 {_num(s.get('target1'))} hit on the way - half booked there")
        lines.append(outcome + (f" · {e(str(s['exit_time']))}" if s.get("exit_time") else ""))
        result = f"Result {_r(s.get('result_r'))} after costs"
        if s.get("cost_r") is not None:
            result += f" (costs {_num(s['cost_r'])}R)"
        lines += [result, "Cycle closed. R = multiples of the planned risk (entry to stop)."]
    return "\n".join(lines)[:4000]


class TelegramPublisher:
    """Posts each new signal once, then each later stage of it once, until it closes."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, str, bool]] = asyncio.Queue(maxsize=200)
        self._task: Optional[asyncio.Task] = None
        self._loaded = False
        self._posted: dict[str, dict[str, Any]] = {}      # key -> {status, msg (Telegram id), at (day)}
        self._last_post = 0.0

    # ── what was posted, and how far each signal got (survives restarts) ──
    @staticmethod
    def _path() -> str:
        return os.path.join(SENT_DIR, "posted.json")

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self._path(), encoding="utf-8") as f:
                self._posted = {k: v for k, v in json.load(f).items() if isinstance(v, dict)}
        except (OSError, ValueError, AttributeError):
            self._posted = {}
        today = date.today().isoformat()
        try:                                   # posted before this file existed: today's ids
            with open(os.path.join(SENT_DIR, f"sent-{today}.json"), encoding="utf-8") as f:
                for key in json.load(f):
                    self._posted.setdefault(key, {"status": "PENDING", "msg": 0, "at": today})
        except (OSError, ValueError, TypeError):
            pass
        cutoff = date.fromordinal(date.today().toordinal() - KEEP_DAYS).isoformat()
        self._posted = {k: v for k, v in self._posted.items() if str(v.get("at", "")) >= cutoff}

    def _save(self) -> None:
        try:
            os.makedirs(SENT_DIR, exist_ok=True)
            tmp = self._path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._posted, f)
            os.replace(tmp, self._path())
        except OSError as e:
            logger.warning("telegram_sent_save_failed", error=str(e))

    # ── public ──
    def publish_new(self, board: str, snap: dict[str, Any], flow: dict[str, Any]) -> int:
        """Queue each signal that is new on its bar (not replayed, not skipped), and each new
        stage of a signal already posted. Returns how many messages were queued."""
        if not enabled():
            return 0
        self._load()
        today = date.today().isoformat()
        queued = 0
        for s in snap.get("feed") or []:
            key = f"{board}:{s.get('symbol')}:{s.get('id')}"
            status = s.get("status")
            record = self._posted.get(key)
            if record is None:
                if status != "PENDING" or not s.get("is_latest_bar") or s.get("backfilled") or s.get("skipped"):
                    continue
                item, record = (key, format_signal(board, s, flow), False), {"status": "PENDING", "msg": 0, "at": today}
            elif STAGE.get(status, -1) > STAGE.get(record.get("status"), 0):
                item = (key, format_update(board, s, record.get("status", "PENDING")), True)
                record = {**record, "status": status, "at": today}
            else:
                continue
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                logger.warning("telegram_queue_full")
                break
            self._posted[key] = record
            queued += 1
        if queued:
            self._save()
            self._ensure_worker()
        return queued

    def _ensure_worker(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._worker(), name="telegram")

    async def _worker(self) -> None:
        while not self._queue.empty():
            key, text, is_update = await self._queue.get()
            wait = MIN_GAP_S - (time.monotonic() - self._last_post)
            if wait > 0:
                await asyncio.sleep(wait)
            record = self._posted.get(key, {})
            message_id = await send(text, reply_to=int(record.get("msg") or 0) if is_update else 0)
            self._last_post = time.monotonic()
            if message_id and not is_update and key in self._posted:
                self._posted[key]["msg"] = message_id        # later stages reply to this post
                self._save()


async def send(text: str, reply_to: int = 0) -> int:
    """Post one message to the channel (a reply when ``reply_to`` is a message id).
    Returns the new message's id, 0 on failure. The token is never logged or returned."""
    token, chat = _config()
    if not token:
        return 0
    payload: dict[str, Any] = {"chat_id": chat, "text": text, "parse_mode": "HTML",
                               "disable_web_page_preview": True, "protect_content": True}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
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
            return 0
        return int((r.json().get("result") or {}).get("message_id") or 0) or 1
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("telegram_send_failed", error=type(e).__name__)   # no URL: it holds the token
        return 0


publisher = TelegramPublisher()


# ── the pinned welcome post: the first thing a new member sees ──
# (a channel cannot greet each member: bots cannot message people first, and a
# "welcome <name>" post would show members to each other)
WELCOME = (
    "<b>Welcome to VerquoAI</b>\n"
    "\n"
    "What is posted here:\n"
    "• <b>Day Trade</b> setups on 5-minute bars and <b>Swing</b> setups on 1-hour bars, as each one "
    "triggers - with why it triggered and its plan (entry, stop, two targets)\n"
    "• With every setup, <b>Money Flow</b>'s top 5 long and top 5 short stocks, each with its futures "
    "open-interest change and build-up\n"
    "• Each setup is then followed to its close, as replies to its post: entry filled, T1 hit, "
    "and the exit (Target 2, stop, or time limit) with its result after costs\n"
    "\n"
    "How to read a post:\n"
    "• <i>Why</i> - the condition that triggered it, on a closed bar\n"
    "• <i>quality</i> - 0-100 score of the setup; <i>volume</i> - versus the recent average\n"
    "• <i>NIFTY options-OI</i> - the index options' bias and how it adjusted the score\n"
    "• <i>Plan (est.)</i> - levels at the time of the signal; prices move on\n"
    "• <i>Result</i> - in R, multiples of the planned risk (entry to stop), after costs\n"
    "\n"
    "Posts come during market hours (9:15-15:30 IST) on trading days. The channel is private and "
    "read-only, and posts cannot be forwarded or saved.\n"
    "\n"
    "<b>VerquoAI will never message you privately, ask for money or payments, or ask for your "
    "broker login or OTP.</b> Anyone who does is not us.\n"
    "\n"
    f"<i>{html.escape(DISCLAIMER)}</i>"
)
WELCOME_FILE = os.path.join(SENT_DIR, "welcome.json")


async def _call(method: str, payload: dict[str, Any]) -> Optional[Any]:
    """One Bot API call to the channel; the result, or None (logged without the token)."""
    token, chat = _config()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{API}/bot{token}/{method}", json={"chat_id": chat, **payload})
        data = r.json() if r.content else {}
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("telegram_call_failed", method=method, error=type(e).__name__)
        return None
    if not data.get("ok"):
        description = str(data.get("description", ""))
        if "message is not modified" not in description:         # an edit to identical text: expected
            logger.warning("telegram_call_failed", method=method, description=description[:120])
        return None
    return data["result"]


async def post_welcome() -> str:
    """Post and pin the welcome message, or update the one already pinned.
    Returns "posted", "updated", "unchanged" or "" on failure."""
    try:
        with open(WELCOME_FILE, encoding="utf-8") as f:
            message_id = int(json.load(f)["message_id"])
    except (OSError, ValueError, KeyError, TypeError):
        message_id = 0
    if message_id:
        result = await _call("editMessageText", {"message_id": message_id, "text": WELCOME, "parse_mode": "HTML",
                                                 "disable_web_page_preview": True})
        if result is not None:
            return "updated"
        # Telegram refuses an edit that changes nothing; any other refusal (deleted post) posts anew
        if await _call("pinChatMessage", {"message_id": message_id, "disable_notification": True}) is not None:
            return "unchanged"
    sent = await _call("sendMessage", {"text": WELCOME, "parse_mode": "HTML", "disable_web_page_preview": True,
                                       "protect_content": True, "disable_notification": True})
    if not sent:
        return ""
    await _call("pinChatMessage", {"message_id": sent["message_id"], "disable_notification": True})
    os.makedirs(SENT_DIR, exist_ok=True)
    with open(WELCOME_FILE, "w", encoding="utf-8") as f:
        json.dump({"message_id": sent["message_id"]}, f)
    return "posted"
