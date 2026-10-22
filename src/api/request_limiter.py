"""Per-account request rate limit for the API (token bucket, in memory).

nginx limits requests per IP; this limits them per signed-in account, so one
account scripting the API from many addresses cannot use up the single worker
that serves everyone (and runs the trading loops).

The defaults sit far above what the dashboard sends: an open tab polls about
1.5 requests/s, and a page load fires a burst of ~20. Five tabs reloaded at
once stay well inside the burst.
"""
from __future__ import annotations

import os
import time

RATE_PER_SECOND = float(os.environ.get("API_RATE_PER_SECOND", "20"))
BURST = float(os.environ.get("API_RATE_BURST", "150"))
_PRUNE_EVERY_S = 300.0


class RequestLimiter:
    def __init__(self, rate: float = RATE_PER_SECOND, burst: float = BURST) -> None:
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, tuple[float, float]] = {}    # key -> (tokens, updated at)
        self._pruned_at = time.monotonic()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        tokens, at = self._buckets.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - at) * self.rate)
        allowed = tokens >= 1.0
        self._buckets[key] = (tokens - 1.0 if allowed else tokens, now)
        if now - self._pruned_at > _PRUNE_EVERY_S:
            self._prune(now)
        return allowed

    def _prune(self, now: float) -> None:
        """Forget buckets that have refilled completely (idle accounts)."""
        full_after = self.burst / self.rate if self.rate > 0 else 0.0
        self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < full_after}
        self._pruned_at = now


api_limiter = RequestLimiter()
