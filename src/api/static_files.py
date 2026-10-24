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
import re
from typing import Any

from starlette.responses import Response
from starlette.staticfiles import StaticFiles

try:
    import rjsmin
except ImportError:          # pragma: no cover - minifying is an extra, not a requirement to run
    rjsmin = None

MINIFY = os.environ.get("STATIC_MINIFY", "1") != "0"

# rjsmin does not know template literals nested inside ``${...}``: it read their text
# as code and dropped its spaces ("11:15 BREAKOUT" became "11:15BREAKOUT", and
# attributes ran together). Template literals are set aside whole before minifying
# and put back unchanged afterwards.
_PLACEHOLDER = "__vqT{}T__"
_PLACEHOLDER_RE = re.compile(r"__vqT(\d+)T__")
_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KEYWORDS = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw",
                   "yield", "await", "instanceof"}


def _skip_string(src: str, i: int) -> int:
    quote, j, n = src[i], i + 1, len(src)
    while j < n and src[j] != quote:
        j += 2 if src[j] == "\\" else 1
    return j + 1


def _skip_regex(src: str, i: int) -> int:
    """End of the regex literal starting at ``i``; -1 when it is not one (a line ends first)."""
    j, n, in_class = i + 1, len(src), False
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            return -1
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            return j
        j += 1
    return -1


def _scan(src: str, i: int, on_template: Any, until_brace: bool = False) -> int:
    """Walk code from ``i`` (to the end, or past the ``}`` that closes a ``${``), calling
    ``on_template(start, end)`` for every template literal found at this level."""
    n, depth, prev, word = len(src), 0, "", ""
    while i < n:
        c = src[i]
        if c in "\"'":
            i, prev, word = _skip_string(src, i), c, ""
            continue
        if c == "/" and src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end < 0 else end
            continue
        if c == "/" and src.startswith("/*", i):
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if c == "/" and (prev == "" or prev in _REGEX_AFTER or word in _REGEX_KEYWORDS):
            end = _skip_regex(src, i)
            if end > 0:
                i, prev, word = end, "/", ""
                continue
        if c == "`":
            end = _skip_template(src, i)
            on_template(i, end)
            i, prev, word = end, "`", ""
            continue
        if until_brace:
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    return i + 1
                depth -= 1
        if not c.isspace():
            word = word + c if (c.isalnum() or c in "_$") and (prev.isalnum() or prev in "_$") else (c if c.isalnum() or c in "_$" else "")
            prev = c
        i += 1
    return n


def _skip_template(src: str, i: int) -> int:
    j, n = i + 1, len(src)
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
        elif c == "`":
            return j + 1
        elif c == "$" and src.startswith("${", j):
            j = _scan(src, j + 2, lambda a, b: None, until_brace=True)
        else:
            j += 1
    return n


def minify_js(source: str) -> str:
    """rjsmin, with every template literal kept exactly as written."""
    templates: list[str] = []
    parts: list[str] = []
    last = 0

    def keep(start: int, end: int) -> None:
        nonlocal last
        parts.append(source[last:start])
        parts.append(f" {_PLACEHOLDER.format(len(templates))} ")
        templates.append(source[start:end])
        last = end

    _scan(source, 0, keep)
    parts.append(source[last:])
    minified = rjsmin.jsmin("".join(parts), keep_bang_comments=False)
    return _PLACEHOLDER_RE.sub(lambda m: templates[int(m.group(1))], minified)


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
            body = minify_js(f.read()).encode()
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
