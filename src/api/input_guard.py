"""One check in front of every API route: request size, JSON shape and the fields that
recur across the app (symbols, exchanges, intervals, day counts, capital, quantities).

Routes still validate what is specific to them; this makes sure that whatever a route
forgets, a hostile value never reaches it:

* bodies over ``MAX_BODY`` bytes get 413 (the largest real body - a strategy definition -
  is a few KB);
* a JSON body that does not parse, or is not an object, gets 400 instead of a server error
  (47 routes read ``request.json()`` and would have raised);
* the shared fields below, in the JSON body or the query string, must match their rule -
  otherwise 400 naming the field. Nothing is silently changed.

Pure ASGI: the body is read once and replayed to the route unchanged.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl

MAX_BODY = 256 * 1024
# routes with a tighter limit of their own (checked here first, so oversized junk gets 413)
ROUTE_MAX_BODY = {"/metrics/events": 32 * 1024, "/api/auth/postback": 64 * 1024, "/telegram/join": 1024}
GUARDED_PREFIXES = ("/api/", "/auth/", "/contact", "/demo/", "/metrics/", "/admin", "/telegram/")

_SYMBOL = re.compile(r"[A-Za-z0-9&._\- ]{1,48}")
_UNDERLYING = re.compile(r"[A-Za-z0-9&_\- ]{1,24}")
EXCHANGES = {"NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD", "NCO"}
INTERVALS = {"minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute", "day", "week", "month"}


def _text(pattern: re.Pattern) -> Callable[[Any], Optional[str]]:
    def check(v: Any) -> Optional[str]:
        if not isinstance(v, str) or not pattern.fullmatch(v.strip() or "x"):
            return "letters, digits and & . - _ only"
        return None
    return check


def _choice(allowed: set[str], upper: bool = False) -> Callable[[Any], Optional[str]]:
    def check(v: Any) -> Optional[str]:
        if not isinstance(v, str) or (v.upper() if upper else v) not in allowed:
            return "must be one of " + ", ".join(sorted(allowed))
        return None
    return check


def _number(lo: float, hi: float, integer: bool = False) -> Callable[[Any], Optional[str]]:
    def check(v: Any) -> Optional[str]:
        if isinstance(v, bool):
            return "must be a number"
        try:
            n = float(v)
        except (TypeError, ValueError):
            return "must be a number"
        if not math.isfinite(n) or not lo <= n <= hi or (integer and n != int(n)):
            return f"must be {'a whole number ' if integer else ''}between {lo:g} and {hi:g}"
        return None
    return check


RULES: dict[str, Callable[[Any], Optional[str]]] = {
    "tradingsymbol": _text(_SYMBOL), "symbol": _text(_SYMBOL), "underlying": _text(_UNDERLYING),
    "exchange": _choice(EXCHANGES, upper=True),
    "interval": _choice(INTERVALS),
    "days": _number(1, 3650, integer=True),
    "bars": _number(10, 20000, integer=True),
    "capital": _number(1000, 1e10), "initial_capital": _number(1000, 1e10),
    "speed": _number(0.1, 1000),
    "quantity": _number(0, 1_000_000, integer=True), "qty": _number(0, 1_000_000, integer=True),
    "lots": _number(0, 10_000, integer=True),
    "limit": _number(1, 5000, integer=True), "offset": _number(0, 1_000_000, integer=True),
    "slippage_pct": _number(0, 10), "commission_pct": _number(0, 10),
    "delta_target": _number(0, 1), "profit_target_pct": _number(0, 1000), "stop_loss_pct": _number(0, 1000),
}


def check_fields(values: dict[str, Any]) -> Optional[str]:
    """The first invalid shared field, as a message (None when all are fine)."""
    for key, value in values.items():
        rule = RULES.get(key)
        if rule is None or value is None or value == "":
            continue
        problem = rule(value)
        if problem:
            return f"Invalid {key}: {problem}."
    return None


class InputGuard:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(GUARDED_PREFIXES):
            await self.app(scope, receive, send)
            return
        query = dict(parse_qsl((scope.get("query_string") or b"").decode("latin-1"), keep_blank_values=False))
        problem = check_fields(query)
        if problem:
            await _reject(send, 400, problem)
            return
        if scope.get("method") not in ("POST", "PUT", "PATCH", "DELETE"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        limit = ROUTE_MAX_BODY.get(scope["path"], MAX_BODY)
        declared = headers.get(b"content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            await _reject(send, 413, "Request too large.")
            return
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                await _reject(send, 413, "Request too large.")
                return
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        body = b"".join(chunks)

        if body.strip() and b"json" in headers.get(b"content-type", b"application/json"):
            try:
                data = json.loads(body)
            except ValueError:
                await _reject(send, 400, "Invalid request: the body is not valid JSON.")
                return
            if not isinstance(data, dict):
                await _reject(send, 400, "Invalid request: expected a JSON object.")
                return
            problem = check_fields(data)
            if problem:
                await _reject(send, 400, problem)
                return

        sent = False

        async def replay() -> Any:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


async def _reject(send: Any, status: int, message: str) -> None:
    payload = json.dumps({"success": False, "error": message, "detail": message, "message": message}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]})
    await send({"type": "http.response.body", "body": payload})
