"""Serialise a shared response once for every request that gets the same object.

View-only users (and demo sessions) are all served the same market-flow and
Day Trade board dicts, refreshed every couple of seconds (src/api/published.py).
Serialising ~340 KB of JSON and gzipping it again for every poll was most of
the server's CPU; here it happens once per refresh, and every poll in between
gets the same bytes.

The body is exactly what FastAPI sends for a ``dict[str, Any]`` route (the same
pydantic serialiser), compressed at the GZipMiddleware's level and threshold.
Entries are keyed by object identity and hold a reference to the object, so an
id is never reused while cached; callers pass objects they do not mutate after
returning them (the published and per-service caches build a new dict per
refresh).
"""
from __future__ import annotations

import gzip
from collections import OrderedDict
from typing import Any

from pydantic import TypeAdapter
from starlette.requests import Request
from starlette.responses import Response

GZIP_MIN_SIZE = 2048        # as app.add_middleware(GZipMiddleware, minimum_size=2048)
GZIP_LEVEL = 9              # GZipMiddleware's default compresslevel
MAX_ENTRIES = 8

_ADAPTER = TypeAdapter(dict[str, Any])
_cache: OrderedDict[int, list[Any]] = OrderedDict()      # id -> [obj, raw bytes, gzip bytes | None]


def _entry(obj: dict[str, Any]) -> list[Any]:
    key = id(obj)
    hit = _cache.get(key)
    if hit is None or hit[0] is not obj:
        hit = [obj, _ADAPTER.dump_json(obj, by_alias=True), None]
        _cache[key] = hit
    _cache.move_to_end(key)
    while len(_cache) > MAX_ENTRIES:
        _cache.popitem(last=False)
    return hit


def shared_json(request: Request, obj: dict[str, Any]) -> Response:
    """``obj`` as a JSON response, serialised and compressed once per object."""
    hit = _entry(obj)
    raw = hit[1]
    if len(raw) >= GZIP_MIN_SIZE and "gzip" in request.headers.get("accept-encoding", ""):
        if hit[2] is None:
            hit[2] = gzip.compress(raw, compresslevel=GZIP_LEVEL)
        return Response(hit[2], media_type="application/json",
                        headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return Response(raw, media_type="application/json")


def clear() -> None:
    _cache.clear()
