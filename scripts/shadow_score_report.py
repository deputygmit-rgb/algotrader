#!/usr/bin/env python3
"""Does the shadow score rank the Day Trade board's signals better than the live quality?

Reads the board's closed signals (net R after costs) from the market-data store and
compares, for the live quality and the shadow score (src/analysis/market_regime.py:
bars scaled to 75 + OI points only when NIFTY and SENSEX agree):

* the top third against the bottom third by score, and the rank correlation;
* results when NIFTY and SENSEX both agree with the trade, both oppose it, or neither;
* coverage: how many signals had an OI reading at all, per hour (the recorder's job).

Read-only. Usage: python scripts/shadow_score_report.py [--days 30] [--db data/market_data.db]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.analysis.market_regime import BIAS_SCORE  # noqa: E402


def load(db: str, days: int) -> list[dict]:
    since = (date.today() - timedelta(days=days)).isoformat()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT session, symbol, setup, side, signal_time, quality, result_r, features FROM signals "
                       "WHERE source='board' AND result_r IS NOT NULL AND session >= ?", (since,)).fetchall()
    con.close()
    seen: dict[tuple, dict] = {}
    for session, symbol, setup, side, t, q, r, f in rows:
        f = json.loads(f or "{}")
        seen.setdefault((session, symbol, setup, side, t), {
            "date": session, "time": t, "side": side, "quality": q, "result_r": r,
            "shadow": f.get("shadow_quality"), "nifty": f.get("market_bias"), "sensex": f.get("sensex_bias"),
        })
    return list(seen.values())


def stat(rs: list[dict]) -> str:
    if not rs:
        return "n=  0"
    a = [r["result_r"] for r in rs]
    se = f" ±{st.stdev(a) / len(a) ** .5:.2f}" if len(a) > 1 else ""
    return f"n={len(a):4d} avg={st.mean(a):+.3f}R{se} win={sum(x > 0 for x in a) / len(a):4.0%}"


def rank_corr(xs: list[float], ys: list[float]) -> float:
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.0] * len(v)
        for rank, i in enumerate(order):
            out[i] = rank
        return out
    if len(xs) < 3 or len(set(xs)) < 2:
        return 0.0
    return st.correlation(ranks(xs), ranks(ys))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--db", default=os.path.join(ROOT, "data", "market_data.db"))
    args = p.parse_args()
    rows = load(args.db, args.days)
    print(f"{len(rows)} closed board signals, {len({r['date'] for r in rows})} sessions, last {args.days} days\n")
    if not rows:
        return

    print("COVERAGE (signals with a NIFTY / SENSEX OI reading at the bar close)")
    by_hour = defaultdict(list)
    for r in rows:
        by_hour[str(r["time"])[:2]].append(r)
    for h in sorted(by_hour):
        rs = by_hour[h]
        print(f"  {h}:00  {len(rs):4d} signals  NIFTY {sum(bool(r['nifty']) for r in rs) / len(rs):4.0%}"
              f"  SENSEX {sum(bool(r['sensex']) for r in rs) / len(rs):4.0%}")

    def agree(r):
        side = 1 if r["side"] == "LONG" else -1
        n = side * BIAS_SCORE.get(str(r["nifty"] or "").upper(), 0.0)
        s = side * BIAS_SCORE.get(str(r["sensex"] or "").upper(), 0.0)
        return "both with" if n > 0 and s > 0 else "both against" if n < 0 and s < 0 else "mixed / neutral / none"

    print("\nNIFTY + SENSEX OI BIAS")
    groups = defaultdict(list)
    for r in rows:
        groups[agree(r)].append(r)
    for k in ("both with", "both against", "mixed / neutral / none"):
        print(f"  {k:24s} {stat(groups[k])}")

    print("\nSCORES (top third vs bottom third)")
    for name, key in (("live quality", "quality"), ("shadow score", "shadow")):
        rs = [r for r in rows if r[key] is not None]
        if len(rs) < 6:
            print(f"  {name:13s} not enough signals with this score yet ({len(rs)})")
            continue
        rs.sort(key=lambda r: r[key])
        k = len(rs) // 3
        rc = rank_corr([r[key] for r in rs], [r["result_r"] for r in rs])
        print(f"  {name:13s} top {stat(rs[-k:])} | bottom {stat(rs[:k])} | rank corr {rc:+.2f} | "
              f"distinct {len({r[key] for r in rs})}")
    print("\nThe shadow score is recorded only; it does not decide which signals are taken.")


if __name__ == "__main__":
    main()
