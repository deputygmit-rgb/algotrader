"""Adaptive setup efficacy: each setup's recent track record shapes its new signals.

The Day Trade and Swing boards fire the same rules every day, whether those
rules have been working or not. This module keeps a rolling record per
(board, setup, side) - the last ``WINDOW_TRADES`` closed trades within
``WINDOW_DAYS`` days, net R after costs - and turns it into:

* a **quality adjustment** of ``-MAX_PENALTY .. +MAX_BONUS`` points, from the
  expectancy *shrunk* towards zero by ``PRIOR_TRADES`` (a handful of lucky
  trades does not make a setup "proven": 12 trades at +0.5R count as +0.19R);
* a **suspension** when the shrunk expectancy is below ``SUSPEND_BELOW`` over
  at least ``MIN_TRADES`` trades. A suspended setup's signals are still shown
  (and tracked to their outcome, so the record keeps updating) but marked
  skipped: out of the stats, the portfolio cap and auto-trade. It is
  reinstated only once the record recovers above ``RESUME_ABOVE`` (hysteresis,
  so it does not flip on every trade).

The record is rebuilt once a day after the close by ``scripts/rebuild_efficacy.py``
(a separate low-priority process) from the stored bars, replayed through the
same engines the boards use, and only from trades closed before that day: the
adjustment applied to today's signals never uses today's outcomes. Each board
freezes a signal's verdict when it first sees it, so a rebuild cannot change a
trade already in progress. Backtests do not use this - applying today's record
to past bars would be look-ahead.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from typing import Any, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.path.join(ROOT, "data", "efficacy.json")

WINDOW_TRADES = 60
WINDOW_DAYS = 120
PRIOR_TRADES = 20
MIN_TRADES = 30
SUSPEND_BELOW = -0.15
RESUME_ABOVE = -0.05
MAX_BONUS, MAX_PENALTY = 8, 15
R_TO_POINTS = 40               # +0.2R shrunk expectancy -> +8 points
RELOAD_EVERY_S = 60.0
# The index options-OI bias adds up to MARKET_WEIGHT quality points to signals with it
# (src/analysis/market_regime.py). Its weight follows its own record: how often a
# directional reading called the index's next OI_HORIZON_MIN minutes, against what
# chance would have scored in the same market (share of rising / falling stretches).
OI_HORIZON_MIN = 30
OI_MIN_READINGS = 300          # directional readings before the bias is trusted or dropped
OI_FULL_EDGE = 0.05            # hit rate this far above chance earns the full weight
OI_UNPROVEN_FACTOR = 0.5       # until then: half weight


def _key(kind: str, setup: str, side: str) -> str:
    return f"{kind}|{setup}|{side}"


def summarize(trades: list[dict[str, Any]], as_of: date, previous: Optional[dict[str, Any]] = None
              ) -> dict[str, dict[str, Any]]:
    """Rolling record per (kind, setup, side) from closed ``trades``
    ({kind, setup, side, date, result_r}); only trades dated before ``as_of``."""
    previous = previous or {}
    since = (as_of - timedelta(days=WINDOW_DAYS)).isoformat()
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        if t.get("result_r") is None or not (since <= str(t["date"]) < as_of.isoformat()):
            continue
        groups.setdefault(_key(t["kind"], t["setup"], t["side"]), []).append(t)
    out: dict[str, dict[str, Any]] = {}
    for key, ts in groups.items():
        ts = sorted(ts, key=lambda t: str(t["date"]))[-WINDOW_TRADES:]
        rs = [float(t["result_r"]) for t in ts]
        n = len(rs)
        avg = sum(rs) / n
        shrunk = sum(rs) / (n + PRIOR_TRADES)
        was_suspended = (previous.get(key) or {}).get("status") == "suspended"
        if n >= MIN_TRADES and (shrunk < SUSPEND_BELOW or (was_suspended and shrunk < RESUME_ABOVE)):
            status = "suspended"
        else:
            status = "active"
        kind, setup, side = key.split("|")
        out[key] = {
            "kind": kind, "setup": setup, "side": side, "n": n,
            "avg_r": round(avg, 3), "shrunk_r": round(shrunk, 3),
            "win_rate": round(sum(r > 0 for r in rs) / n * 100),
            "adj": max(-MAX_PENALTY, min(MAX_BONUS, int(round(shrunk * R_TO_POINTS)))),
            "status": status, "since": str(ts[0]["date"]), "until": str(ts[-1]["date"]),
        }
    return out


def oi_bias_record(rows: list[tuple[str, str, float]], horizon_min: int = OI_HORIZON_MIN) -> dict[str, Any]:
    """Track record of the per-minute OI bias: ``rows`` are (ts, bias, spot) in time order."""
    from datetime import datetime as _dt

    from src.analysis.market_regime import BIAS_SCORE

    spot = {str(ts)[:16]: float(sp) for ts, _, sp in rows if sp}
    moves = []
    for ts, bias, sp in rows:
        if not sp:
            continue
        later = spot.get((_dt.fromisoformat(str(ts)[:16]) + timedelta(minutes=horizon_min)).isoformat()[:16])
        if later is not None:
            moves.append((BIAS_SCORE.get(str(bias or "").upper(), 0.0), later / float(sp) - 1))
    directional = [(score, mv) for score, mv in moves if score and mv]
    n = len(directional)
    if not n:
        return {"n": 0, "factor": OI_UNPROVEN_FACTOR, "status": "no readings"}
    moving = [mv for _, mv in moves if mv]
    up_share = sum(mv > 0 for mv in moving) / len(moving)
    hits = sum((score > 0) == (mv > 0) for score, mv in directional) / n
    chance = sum(up_share if score > 0 else 1 - up_share for score, _ in directional) / n
    edge = hits - chance
    if n < OI_MIN_READINGS:
        factor, status = OI_UNPROVEN_FACTOR, "unproven"
    else:
        factor = max(0.0, min(1.0, edge / OI_FULL_EDGE))
        status = "trusted" if factor >= 1 else "dropped" if factor <= 0 else "partial"
    return {"n": n, "hit_rate": round(hits, 3), "chance": round(chance, 3), "edge": round(edge, 3),
            "factor": round(factor, 2), "status": status, "horizon_min": horizon_min}


def save(records: dict[str, dict[str, Any]], as_of: date, path: str = PATH, note: str = "",
         oi_bias: Optional[dict[str, Any]] = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"as_of": as_of.isoformat(), "note": note, "records": records, "oi_bias": oi_bias or {}}, f, indent=1)
    os.replace(tmp, path)


class Efficacy:
    """The current record, read from ``path`` (re-read when the file changes)."""

    def __init__(self, path: str = PATH) -> None:
        self.path = path
        self.as_of: Optional[str] = None
        self.records: dict[str, dict[str, Any]] = {}
        self.oi_bias: dict[str, Any] = {}
        self._mtime = 0.0
        self._checked = 0.0

    def _refresh(self) -> None:
        now = time.monotonic()
        if now - self._checked < RELOAD_EVERY_S and self._checked:
            return
        self._checked = now
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.records, self.as_of, self._mtime = dict(data.get("records") or {}), data.get("as_of"), mtime
            self.oi_bias = dict(data.get("oi_bias") or {})
        except (OSError, ValueError):
            pass

    def lookup(self, kind: str, setup: str, side: str) -> Optional[dict[str, Any]]:
        self._refresh()
        return self.records.get(_key(kind, setup, side))

    def verdict(self, kind: str, signal: dict[str, Any], min_quality: float) -> dict[str, Any]:
        """{n, avg_r, shrunk_r, adj, status, quality, skip} for one signal."""
        rec = self.lookup(kind, signal["setup"], signal["side"])
        base = int(signal.get("quality") or 0)
        if not rec:
            return {"n": 0, "avg_r": None, "shrunk_r": None, "adj": 0, "status": "no record",
                    "quality": base, "skip": None, "as_of": self.as_of}
        quality = max(0, min(100, base + rec["adj"]))
        skip = None
        if rec["status"] == "suspended":
            skip = f"setup suspended: {rec['avg_r']:+.2f}R a trade over its last {rec['n']}"
        elif quality < min_quality:
            skip = f"weak lately: {rec['avg_r']:+.2f}R a trade over its last {rec['n']} takes quality to {quality}"
        return {"n": rec["n"], "avg_r": rec["avg_r"], "shrunk_r": rec["shrunk_r"], "adj": rec["adj"],
                "status": rec["status"], "quality": quality, "skip": skip, "as_of": self.as_of}

    def oi_weight(self, underlying: str = "NIFTY") -> float:
        """Quality points a full BULLISH / BEARISH agreement is worth now, by the bias's record."""
        from src.analysis.market_regime import MARKET_WEIGHT

        self._refresh()
        rec = self.oi_bias.get(underlying) or {}
        return round(MARKET_WEIGHT * float(rec.get("factor", OI_UNPROVEN_FACTOR)), 2)

    def summary(self, kind: str) -> list[dict[str, Any]]:
        self._refresh()
        rows = [r for r in self.records.values() if r["kind"] == kind]
        return sorted(rows, key=lambda r: (r["setup"], r["side"]))


_EFFICACY: Optional[Efficacy] = None


def get_efficacy() -> Efficacy:
    global _EFFICACY
    if _EFFICACY is None:
        _EFFICACY = Efficacy()
    return _EFFICACY


def apply_efficacy(kind: str, feed: list[dict[str, Any]], frozen: dict[str, dict[str, Any]],
                   min_quality: float, efficacy: Optional[Efficacy] = None) -> None:
    """Attach each signal's verdict (frozen at first sight) and mark skips."""
    eff = efficacy or get_efficacy()
    for s in feed:
        v = frozen.get(s["id"])
        if v is None:
            v = frozen[s["id"]] = eff.verdict(kind, s, min_quality)
        s["efficacy"] = v
        if v["adj"]:
            s["base_quality_board"] = s["quality"]
            s["quality"] = v["quality"]
        if v["skip"] and not s.get("skipped"):
            s["skipped"] = v["skip"]


# ── nightly rebuild (a separate, low-priority process) ───────────────────
REBUILD_AFTER = (16, 10)          # IST, once the session's bars are complete
REBUILD_CHECK_S = 600.0
STARTUP_DELAY_S = 120.0


def _stale(today: date, now_hm: tuple[int, int], as_of: Optional[str]) -> bool:
    """Missing, older than yesterday, or not yet rebuilt today after the close."""
    if not as_of:
        return True
    if as_of < (today - timedelta(days=1)).isoformat():
        return True
    return as_of < today.isoformat() and now_hm >= REBUILD_AFTER


async def rebuild_once() -> bool:
    import asyncio
    import sys

    from src.utils.logger import get_logger

    log = get_logger(__name__)
    script = os.path.join(ROOT, "scripts", "rebuild_efficacy.py")
    kwargs: dict[str, Any] = {}
    if os.name == "posix":
        kwargs["preexec_fn"] = lambda: os.nice(10)        # behind the web worker for CPU
    proc = await asyncio.create_subprocess_exec(sys.executable, script, "--email", cwd=ROOT,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
    out, err = await proc.communicate()
    if proc.returncode == 0:
        log.info("efficacy_rebuilt", result=out.decode(errors="replace").strip()[-200:])
        return True
    log.warning("efficacy_rebuild_failed", code=proc.returncode, error=err.decode(errors="replace").strip()[-400:])
    return False


async def run_rebuilder() -> None:
    import asyncio
    from datetime import datetime

    await asyncio.sleep(STARTUP_DELAY_S)
    while True:
        try:
            eff = get_efficacy()
            eff._checked = 0.0
            eff._refresh()
            now = datetime.now()
            if _stale(now.date(), (now.hour, now.minute), eff.as_of):
                await rebuild_once()
        except Exception as e:      # never let the scheduler die
            from src.utils.logger import get_logger

            get_logger(__name__).warning("efficacy_scheduler_failed", error=str(e))
        await asyncio.sleep(REBUILD_CHECK_S)
