"""Efficacy report for the 5-minute day-trade setups.

Two sources, both net of slippage and Zerodha-style charges:

* **replay** - every stock with stored 5-minute bars in ``data/market_data.db``
  is replayed through ``replay_sessions`` (the engine behind the Day Trade
  board, backtests, paper and live). Results are split into an earlier
  (in-sample) and a later (out-of-sample) half by date, so a rule that only
  works in one half shows up as such.
* **flow** - replayed signals split by the money-flow bias stored at their bar
  close (``flow_snapshots``): the board's direction filter, tested on history.
* **ledger** - the board's own record of every signal it saw live (the
  ``signals`` table), excluding signals replayed from before a stock was
  tracked.

Usage:
    python scripts/day_trade_efficacy.py                       # replay + ledgers
    python scripts/day_trade_efficacy.py --split 2026-09-01    # out-of-sample from this date
    python scripts/day_trade_efficacy.py --setups ORB,PD_BREAK,VWAP_RECLAIM,SQUEEZE,PULLBACK
    python scripts/day_trade_efficacy.py --no-replay           # ledger only
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from typing import Any, Callable, Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

from src.strategy.day_trade_setups import MIN_HISTORY_SESSIONS, replay_sessions  # noqa: E402


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Expectancy, its standard error, win rate and profit factor over closed trades."""
    rs = [t["result_r"] for t in trades if t.get("result_r") is not None]
    if not rs:
        return {"n": 0}
    gross = [t["gross_r"] for t in trades if t.get("gross_r") is not None]
    wins, losses = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    return {
        "n": len(rs),
        "win_rate": sum(r > 0 for r in rs) / len(rs) * 100,
        "avg_r": statistics.mean(rs),
        "se_r": statistics.pstdev(rs) / math.sqrt(len(rs)),
        "gross_r": statistics.mean(gross) if gross else None,
        "cost_r": statistics.mean(t.get("cost_r") or 0 for t in trades),
        "pf": wins / losses if losses else math.inf,
        "total_r": sum(rs),
    }


def fmt(m: dict[str, Any]) -> str:
    if not m.get("n"):
        return "n=0"
    if m["n"] < 10:
        return f"n={m['n']:<4d} (too few)"
    return (f"n={m['n']:<4d} win {m['win_rate']:5.1f}%  net {m['avg_r']:+.3f}R ±{m['se_r']:.3f}  "
            f"gross {m['gross_r']:+.3f}R  cost {m['cost_r']:.3f}R  PF {m['pf']:.2f}  total {m['total_r']:+.1f}R")


def breakdown(title: str, trades: list[dict[str, Any]], key: Callable[[dict[str, Any]], Any],
              halves: tuple[list, list] | None = None) -> None:
    print(f"\n  by {title}")
    groups: dict[Any, list] = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    for k in sorted(groups, key=str):
        line = f"    {str(k):<16} {fmt(summarize(groups[k]))}"
        if halves:
            a = summarize([t for t in halves[0] if key(t) == k])
            b = summarize([t for t in halves[1] if key(t) == k])
            if a.get("n", 0) >= 10 and b.get("n", 0) >= 10:
                line += f"   | IS {a['avg_r']:+.3f} / OOS {b['avg_r']:+.3f}"
        print(line)


def replay(db: str, setups: list[str] | None, split: str | None) -> list[dict[str, Any]]:
    con = sqlite3.connect(db)
    tokens = [r[0] for r in con.execute(
        "SELECT instrument_token FROM candles WHERE interval='5minute' GROUP BY 1 "
        "HAVING COUNT(DISTINCT substr(ts, 1, 10)) > ?", (MIN_HISTORY_SESSIONS,))]
    from scripts.strategy_benchmark import symbol_names

    names = symbol_names(db)
    trades: list[dict[str, Any]] = []
    sessions = 0
    for token in tokens:
        df = pd.read_sql("SELECT ts AS timestamp, open, high, low, close, volume FROM candles "
                         "WHERE interval='5minute' AND instrument_token=? ORDER BY ts", con, params=(token,))
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        result = replay_sessions(df, setups=setups)
        sessions += result["sessions"]
        for s in result["signals"]:
            if s.get("result_r") is not None:
                trades.append({**s, "symbol": names.get(token) or str(token)})
    con.close()
    if not trades:
        print("replay: no stock has enough stored 5-minute history")
        return trades
    dates = sorted({t["date"] for t in trades})
    split = split or dates[len(dates) // 2]
    first = [t for t in trades if t["date"] < split]
    second = [t for t in trades if t["date"] >= split]
    print(f"REPLAY  {len(tokens)} stocks · {sessions} stock-sessions · {dates[0]} → {dates[-1]} · "
          f"{len(trades) / max(sessions, 1):.2f} trades per stock-session")
    print(f"  all              {fmt(summarize(trades))}")
    print(f"  in-sample  <{split} {fmt(summarize(first))}")
    print(f"  out-sample ≥{split} {fmt(summarize(second))}")
    halves = (first, second)
    breakdown("setup", trades, lambda t: t["setup"], halves)
    breakdown("side", trades, lambda t: t["side"], halves)
    breakdown("exit", trades, lambda t: t["status"])
    breakdown("signal hour", trades, lambda t: t["time"][:2], halves)
    breakdown("quality", trades, lambda t: f"{t['quality'] // 10 * 10}-{t['quality'] // 10 * 10 + 9}", halves)
    mfe = [t["mfe_r"] for t in trades]
    print(f"\n  excursion: median MFE {statistics.median(mfe):+.2f}R · median MAE "
          f"{statistics.median(t['mae_r'] for t in trades):+.2f}R · reached 1R {sum(m >= 1 for m in mfe) / len(mfe):.0%}"
          f" · T1 {sum(m >= 1.5 for m in mfe) / len(mfe):.0%} · T2 {sum(m >= 3 for m in mfe) / len(mfe):.0%}")
    return trades


def flow_filter(db: str, trades: list[dict[str, Any]]) -> None:
    """Replayed signals split by the money-flow bias stored at their bar close."""
    con = sqlite3.connect(db)
    have = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM flow_snapshots").fetchone()
    if not have[0]:
        con.close()
        print("\nFLOW    no flow snapshots stored yet: they are recorded every 5-minute bar close while the "
              "market is open, and this section fills in as sessions accumulate")
        return
    bias = {(sym, ts): b for sym, ts, b in con.execute("SELECT symbol, ts, bias FROM flow_snapshots")}
    con.close()
    tagged = []
    for t in trades:
        b = bias.get((t["symbol"], f"{t['date']}T{t['time']}"))
        if b is None:
            continue
        t = {**t, "flow": "with flow" if b == t["side"] else "against flow" if b in ("LONG", "SHORT") else "neutral"}
        tagged.append(t)
    print(f"\nFLOW    {have[0]} snapshot rows {have[1]} → {have[2]} · {len(tagged)} replayed signals have a snapshot")
    if tagged:
        breakdown("money-flow bias at the signal", tagged, lambda t: t["flow"])


def ledger(db: str) -> None:
    """The board's own record of the signals it saw live (``signals`` table)."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM signals WHERE strategy = 'day_trade_setups'")]
    con.close()
    for account in sorted({r["account"] for r in rows}) or [None]:
        mine = [r for r in rows if r["account"] == account]
        live = [r for r in mine if not r["backfilled"] and not r["skipped"]]
        print(f"\nLEDGER  {account or '(no signals recorded yet)'}: {len(mine)} signals seen, {len(live)} tradeable "
              f"({sum(1 for r in mine if r['backfilled'])} replayed, {sum(1 for r in mine if r['skipped'])} skipped)")
        if not live:
            continue
        print(f"  all              {fmt(summarize(live))}")
        breakdown("setup", live, lambda t: t["setup"])
        breakdown("side", live, lambda t: t["side"])
        breakdown("money-flow bias", live, lambda t: "with flow" if t["flow_bias"] == t["side"] else t["flow_bias"] or "none")
        breakdown("session", live, lambda t: t["session"])
        decisions: dict[str, int] = defaultdict(int)
        for r in mine:
            decisions[r["decision"] or "none"] += 1
        print("  auto-trade decisions: " + ", ".join(f"{k} {v}" for k, v in sorted(decisions.items())))


def main(argv: Iterable[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "market_data.db"))
    ap.add_argument("--setups", help="comma-separated setups to replay (default: the engine's DEFAULT_SETUPS)")
    ap.add_argument("--split", help="first out-of-sample date, YYYY-MM-DD (default: the median trade date)")
    ap.add_argument("--no-replay", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    from src.data.market_data_store import MarketDataStore

    MarketDataStore(db_path=args.db).close()     # creates the flow_snapshots / signals tables on older databases
    if not args.no_replay:
        trades = replay(args.db, args.setups.split(",") if args.setups else None, args.split)
        flow_filter(args.db, trades)
    ledger(args.db)


if __name__ == "__main__":
    main()
