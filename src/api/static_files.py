"""Static files, with scripts served minified: no comments and no layout, so
the page's code does not ship its own explanations of how the product works.

Each script is minified once per change (keyed by path, size and mtime) and
answered with an ETag, so browsers revalidate cheaply. ``STATIC_MINIFY=0``
serves the files as written (for debugging in a browser); without the
``rjsmin`` package they are served as written too.
"""
from __future__ import annotations

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
            with open(full, encoding="utf-8") as f:
                body = rjsmin.jsmin(f.read(), keep_bang_comments=False).encode()
            hit = (key, body, '"' + hashlib.sha256(body).hexdigest()[:20] + '"')
            self._cache[full] = hit
        _, body, etag = hit
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        request_headers = dict(scope.get("headers") or [])
        if request_headers.get(b"if-none-match", b"").decode() == etag:
            return Response(status_code=304, headers=headers)
        return Response(body if scope["method"] == "GET" else b"", media_type="text/javascript", headers=headers)
