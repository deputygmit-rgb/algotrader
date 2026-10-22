"""Efficacy report for the swing setups (1-hour bars, daily context).

Replays ``swing_setups.analyze_swing`` - the engine behind the Swing board -
over every stock with stored 1-hour and daily bars in ``data/market_data.db``.
Signals are looked for once 50 daily bars exist (the daily EMA 50). Results are
net of slippage and delivery charges, split by setup and side and into an
earlier and a later half by date, so a rule that only worked in one half
shows up as such.

    python scripts/swing_efficacy.py
    python scripts/swing_efficacy.py --db path/to/market_data.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.analysis.swing_setups import analyze_swing  # noqa: E402


def line(rs: list[float]) -> str:
    if not rs:
        return "n=   0"
    return (f"n={len(rs):4d}  avg {statistics.mean(rs):+.3f}R  total {sum(rs):+7.1f}R  "
            f"win {sum(r > 0 for r in rs) / len(rs):4.0%}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "market_data.db"))
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    hourly_tokens = {r[0] for r in con.execute(
        "SELECT instrument_token FROM candles WHERE interval='60minute' GROUP BY 1 HAVING COUNT(*) > 300")}
    daily_tokens = {r[0] for r in con.execute(
        "SELECT instrument_token FROM candles WHERE interval='day' GROUP BY 1 HAVING COUNT(*) > 70")}
    trades = []
    for token in sorted(hourly_tokens & daily_tokens):
        bars = {}
        for interval in ("60minute", "day"):
            bars[interval] = [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in con.execute(
                "SELECT ts, open, high, low, close, volume FROM candles WHERE interval=? AND instrument_token=? "
                "ORDER BY ts", (interval, token))]
        daily = bars["day"]
        first = str(daily[50]["t"])[:10]
        start = next((i for i, b in enumerate(bars["60minute"]) if str(b["t"])[:10] >= first), len(bars["60minute"]))
        out = analyze_swing(bars["60minute"], daily, start_index=start)
        trades += [{**s, "token": token} for s in out["signals"] if s.get("result_r") is not None]
    con.close()
    if not trades:
        print("no stock has enough stored 1-hour and daily bars")
        return
    dates = sorted(t["time"][:10] for t in trades)
    mid = dates[len(dates) // 2]
    print(f"SWING REPLAY  {len({t['token'] for t in trades})} stocks · {len(trades)} closed trades · "
          f"{dates[0]} → {dates[-1]} · net of slippage and delivery charges")
    print(f"  all             {line([t['result_r'] for t in trades])}")
    print(f"  before {mid}  {line([t['result_r'] for t in trades if t['time'][:10] < mid])}")
    print(f"  from {mid}    {line([t['result_r'] for t in trades if t['time'][:10] >= mid])}")
    groups = defaultdict(list)
    for t in trades:
        groups[(t["setup"], t["side"])].append(t["result_r"])
    print("\n  setup / side")
    for (setup, side), rs in sorted(groups.items()):
        print(f"    {setup:15} {side:5}  {line(rs)}")
    exits = defaultdict(list)
    for t in trades:
        exits[t["status"]].append(t["result_r"])
    print("\n  exit")
    for k, rs in sorted(exits.items()):
        print(f"    {k:11}  {line(rs)}")


if __name__ == "__main__":
    main()
