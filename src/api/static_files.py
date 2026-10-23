"""Static files, with scripts served minified: no comments and no layout, so
the page's code does not ship its own explanations of how the product works.

Each script is minified once per change (keyed by path, size and mtime) and
answered with an ETag, so browsers revalidate cheaply. ``STATIC_MINIFY=0``
serves the files as written (for debugging in a browser); without the
``rjsmin`` package they are served as written too.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any

from starlette.responses import Response
from starlette.staticfiles import StaticFiles

try:
    import rjsmin
except ImportError:          # pragma: no cover - minifying is an extra, not a requirement to run
    rjsmin = None

MINIFY = os.environ.get("STATIC_MINIFY", "1") != "0"


class MinifiedStaticFiles(StaticFiles):
    def __init__(self, *args: Any, minify: bool = MINIFY, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.minify = minify and rjsmin is not None
        self._cache: dict[str, tuple[tuple[int, int], bytes, str]] = {}

    async def get_response(self, path: str, scope: Any) -> Response:
        if not (self.minify and path.endswith(".js") and scope.get("method") in ("GET", "HEAD")):
            return await super().get_response(path, scope)
        full, stat = self.lookup_path(path)
        if stat is None or not os.path.isfile(full):
            return await super().get_response(path, scope)        # its usual 404
        key = (stat.st_size, int(stat.st_mtime_ns))
        hit = self._cache.get(full)
        if hit is None or hit[0] != key:
            # off the event loop: after a restart the first page load asks for every script at once
            hit = await asyncio.to_thread(self._minified, full, key)
        _, body, etag = hit
        # pages link scripts as name.js?v=<mtime>: a versioned URL never changes, so the
        # browser keeps it without asking again (about 20 round trips saved per page load)
        versioned = b"v=" in (scope.get("query_string") or b"")
        headers = {"ETag": etag,
                   "Cache-Control": "public, max-age=31536000, immutable" if versioned else "no-cache"}
        request_headers = dict(scope.get("headers") or [])
        if request_headers.get(b"if-none-match", b"").decode() == etag:
            return Response(status_code=304, headers=headers)
        return Response(body if scope["method"] == "GET" else b"", media_type="text/javascript", headers=headers)

    def _minified(self, full: str, key: tuple[int, int]) -> tuple[tuple[int, int], bytes, str]:
        with open(full, encoding="utf-8") as f:
            body = rjsmin.jsmin(f.read(), keep_bang_comments=False).encode()
        hit = (key, body, '"' + hashlib.sha256(body).hexdigest()[:20] + '"')
        self._cache[full] = hit
        return hit

    def warm(self) -> int:
        """Minify every script now (call in a thread at startup): the first visitor after a
        restart then gets them from the cache. Returns how many were minified."""
        if not self.minify:
            return 0
        done = 0
        for directory in self.all_directories:
            for root, _dirs, files in os.walk(directory):
                for name in files:
                    if name.endswith(".js"):
                        full = os.path.join(root, name)
                        st = os.stat(full)
                        self._minified(full, (st.st_size, int(st.st_mtime_ns)))
                        done += 1
        return done
