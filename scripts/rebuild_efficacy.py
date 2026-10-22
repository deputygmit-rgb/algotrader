"""Rebuild the setups' rolling track record (data/efficacy.json).

Replays the stored bars in ``data/market_data.db`` through the engines the
boards use - 5-minute bars through the Day Trade setups, 1-hour + daily bars
through the Swing setups - and keeps, per (board, setup, side), the last
closed trades before today (``src/analysis/efficacy.py`` for the rules).

The web app runs this once a day after the close as a separate low-priority
process (it reads a lot of history); run it by hand any time:

    python scripts/rebuild_efficacy.py
    python scripts/rebuild_efficacy.py --show          # print the current record
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.analysis.efficacy import PATH, WINDOW_DAYS, save, summarize  # noqa: E402

DAY_HISTORY_DAYS = WINDOW_DAYS + 45        # warm-up: the setups need 15 earlier sessions
SWING_DAILY_DAYS = WINDOW_DAYS + 120       # daily EMA 50 + 20-day levels before the window


def day_trades(con: sqlite3.Connection, today: date) -> list[dict]:
    import pandas as pd

    from src.strategy.day_trade_setups import MIN_HISTORY_SESSIONS, replay_sessions

    since = (today - timedelta(days=DAY_HISTORY_DAYS)).isoformat()
    tokens = [r[0] for r in con.execute(
        "SELECT instrument_token FROM candles WHERE interval='5minute' AND ts >= ? GROUP BY 1 "
        "HAVING COUNT(DISTINCT substr(ts, 1, 10)) > ?", (since, MIN_HISTORY_SESSIONS))]
    out = []
    for token in tokens:
        df = pd.read_sql("SELECT ts AS timestamp, open, high, low, close, volume FROM candles "
                         "WHERE interval='5minute' AND instrument_token=? AND ts >= ? ORDER BY ts",
                         con, params=(token, since))
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        for s in replay_sessions(df)["signals"]:
            if s.get("result_r") is not None and s["date"] < today.isoformat():
                out.append({"kind": "day", "setup": s["setup"], "side": s["side"], "date": s["date"],
                            "result_r": s["result_r"]})
    return out


def swing_trades(con: sqlite3.Connection, today: date) -> list[dict]:
    from src.analysis.swing_setups import analyze_swing

    since_daily = (today - timedelta(days=SWING_DAILY_DAYS)).isoformat()
    since_hourly = (today - timedelta(days=WINDOW_DAYS + 30)).isoformat()
    hourly = {r[0] for r in con.execute(
        "SELECT instrument_token FROM candles WHERE interval='60minute' AND ts >= ? GROUP BY 1 HAVING COUNT(*) > 100",
        (since_hourly,))}
    daily = {r[0] for r in con.execute(
        "SELECT instrument_token FROM candles WHERE interval='day' AND ts >= ? GROUP BY 1 HAVING COUNT(*) > 70",
        (since_daily,))}
    out = []
    for token in sorted(hourly & daily):
        rows = {}
        for interval, since in (("60minute", since_hourly), ("day", since_daily)):
            rows[interval] = [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in con.execute(
                "SELECT ts, open, high, low, close, volume FROM candles WHERE interval=? AND instrument_token=? "
                "AND ts >= ? ORDER BY ts", (interval, token, since))]
        d = rows["day"]
        first = str(d[min(50, len(d) - 1)]["t"])[:10]
        start = next((i for i, b in enumerate(rows["60minute"]) if str(b["t"])[:10] >= first), len(rows["60minute"]))
        for s in analyze_swing(rows["60minute"], d, start_index=start)["signals"]:
            # dated by the exit: a trade counts once its outcome is known, before today
            if s.get("result_r") is not None and s.get("exit_time") and s["exit_time"][:10] < today.isoformat():
                out.append({"kind": "swing", "setup": s["setup"], "side": s["side"], "date": s["exit_time"][:10],
                            "result_r": s["result_r"]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "market_data.db"))
    ap.add_argument("--out", default=PATH)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    if args.show:
        with open(args.out, encoding="utf-8") as f:
            data = json.load(f)
        print(f"as of {data['as_of']}")
        for r in sorted(data["records"].values(), key=lambda r: (r["kind"], r["setup"], r["side"])):
            print(f"  {r['kind']:5} {r['setup']:15} {r['side']:5} n={r['n']:3d} avg {r['avg_r']:+.3f}R "
                  f"shrunk {r['shrunk_r']:+.3f}R win {r['win_rate']:3d}%  adj {r['adj']:+d}  {r['status']}")
        return
    started = time.time()
    today = date.today()
    previous = {}
    try:
        with open(args.out, encoding="utf-8") as f:
            previous = json.load(f).get("records") or {}
    except (OSError, ValueError):
        pass
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    trades = day_trades(con, today) + swing_trades(con, today)
    con.close()
    records = summarize(trades, today, previous)
    save(records, today, args.out, note=f"{len(trades)} trades replayed in {time.time() - started:.0f}s")
    print(f"efficacy: {len(records)} setup records from {len(trades)} trades in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
