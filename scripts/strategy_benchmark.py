"""Benchmark every supported single-stock strategy on one footing.

Each strategy runs through the shared ``BacktestEngine`` exactly as the app's
"Test Strategy" does (₹1,00,000, positions sized to risk 1% of capital,
Indian costs, next-bar-open fills, the strategy's own exit plan) on every
stock with stored bars for each timeframe it supports. Per strategy and
timeframe it reports:

* stocks tested, trades, pooled win rate and profit factor (all trades of all
  stocks together, so one lucky stock does not carry the row);
* median net return per stock, and the median buy-and-hold return of the
  same stocks over the same bars - the benchmark it has to beat;
* the out-of-sample result: P&L of trades entered in the last 30% of each
  window's calendar time;
* a time-based walk-forward: each window is cut into ``FOLDS`` equal calendar
  periods and every trade is credited to the period it was entered in, so a
  strategy has to make money period after period, not just on average;
* live record: closed trades with ``source = 'live'`` in every account's
  journal (``data/users/*/journal.db``).

Results go to ``data/strategy_benchmark.json`` (read by the API) and stdout.

Usage:
    python scripts/strategy_benchmark.py                    # all strategies, up to 8 stocks per timeframe
    python scripts/strategy_benchmark.py --stocks 20 --workers 6
    python scripts/strategy_benchmark.py --only rsi,macd_crossover
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

DB = os.path.join(ROOT, "data", "market_data.db")
OUT = os.path.join(ROOT, "data", "strategy_benchmark.json")
CAPITAL = 100_000.0
RISK_PER_TRADE = 0.01      # as the app's stock test (TradingService.TEST_RISK_PER_TRADE)
OOS_FRACTION = 0.3         # as TradingService.OOS_FRACTION, measured in calendar time
FOLDS = 3                  # equal calendar periods of the time-based walk-forward
# bars of each timeframe a test covers (the app's STOCK_TEST_TIMEFRAMES windows, in calendar days)
WINDOW_DAYS = {"5minute": 100, "15minute": 200, "60minute": 400, "day": 2000}
MIN_BARS = 150


def strategy_catalog() -> dict[str, dict[str, Any]]:
    """name -> {label, group, cls, timeframes} for every built-in single-stock strategy."""
    from src.api.service import TradingService
    from src.strategy.analysis_strategies import ANALYSIS_STRATEGIES, ANALYSIS_STRATEGY_LABELS

    classes = TradingService._equity_strategy_classes()
    out: dict[str, dict[str, Any]] = {}
    for name, label in TradingService.EQUITY_STRATEGY_LABELS.items():
        out[name] = {"label": label, "group": "Equity", "cls": classes[name]}
    for name, cls in ANALYSIS_STRATEGIES.items():
        out[name] = {"label": ANALYSIS_STRATEGY_LABELS.get(name, name), "group": "Analysis", "cls": cls}
    for meta in out.values():
        required = getattr(meta["cls"], "required_interval", None)
        meta["timeframes"] = [required] if required else list(WINDOW_DAYS)
        meta["description"] = (meta["cls"].__doc__ or "").strip().split("\n\n")[0].replace("\n", " ")
    return out


def symbol_names(db: str = DB) -> dict[int, str]:
    """instrument token -> trading symbol, from the instruments table, else the scanner caches."""
    import gzip

    names: dict[int, str] = {256265: "NIFTY 50"}

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if isinstance(o.get("token"), int) and isinstance(o.get("symbol"), str):
                names.setdefault(o["token"], o["symbol"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for path in glob.glob(os.path.join(ROOT, "data", "scanner_cache", "*.json.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                walk(json.load(f))
        except (OSError, ValueError):
            continue
    con = sqlite3.connect(db)
    names.update({int(t): s for t, s in con.execute(
        "SELECT instrument_token, tradingsymbol FROM instruments WHERE tradingsymbol != ''")})
    con.close()
    return names


def stocks_for(interval: str, limit: int) -> list[tuple[int, str]]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT instrument_token, COUNT(*) n FROM candles WHERE interval = ? "
        "GROUP BY instrument_token HAVING n >= ? ORDER BY n DESC LIMIT ?",
        (interval, MIN_BARS, limit + 1)).fetchall()
    con.close()
    names = symbol_names()
    # the NIFTY index has no volume and cannot be traded as a stock
    return [(int(t), names.get(int(t), str(t))) for t, _ in rows if int(t) != 256265][:limit]


def run_one(name: str, interval: str, token: int, symbol: str) -> dict[str, Any]:
    """One strategy on one stock and timeframe (runs in a worker process)."""
    from src.data.backtest import BacktestEngine

    meta = strategy_catalog()[name]
    con = sqlite3.connect(DB)
    df = pd.read_sql("SELECT ts AS timestamp, open, high, low, close, volume FROM candles "
                     "WHERE interval = ? AND instrument_token = ? ORDER BY ts", con, params=(interval, token))
    con.close()
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed")
    df = df.set_index("timestamp")
    end = df.index[-1]
    df = df[df.index >= end - pd.Timedelta(days=WINDOW_DAYS[interval])]
    if len(df) < MIN_BARS:
        return {"error": "too few bars"}
    engine = BacktestEngine(strategy=meta["cls"](None), initial_capital=CAPITAL,
                            position_sizing="risk_based", risk_per_trade=RISK_PER_TRADE)
    raw = engine.run(df, instrument_token=token, tradingsymbol=symbol, timeframe=interval)
    if "error" in raw:
        return {"error": raw["error"]}
    start = df.index[0]
    span = end - start
    split = str(start + span * (1 - OOS_FRACTION))[:16]
    edges = [str(start + span * k / FOLDS)[:16] for k in range(1, FOLDS)]
    closed = [t for t in raw.get("trades", []) if "pnl" in t]
    oos = [t for t in closed if str(t.get("entry_time", ""))[:16] >= split]
    fold_pnl = [0.0] * FOLDS
    for t in closed:
        fold_pnl[sum(str(t.get("entry_time", ""))[:16] >= e for e in edges)] += t["pnl"]
    return {
        "symbol": symbol, "bars": len(df), "start": str(df.index[0])[:10], "end": str(end)[:10],
        "return_pct": raw["total_return_pct"], "max_dd_pct": raw.get("max_drawdown_pct", 0.0),
        "sharpe": raw.get("sharpe_ratio", 0.0),
        "buy_hold_pct": round((df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100, 2),
        "pnls": [round(t["pnl"], 2) for t in closed],
        "oos_pnl": round(sum(t["pnl"] for t in oos), 2), "oos_trades": len(oos),
        "fold_pnl": [round(p, 2) for p in fold_pnl],
        "costs": raw.get("total_costs", 0.0),
    }


def live_record() -> dict[str, dict[str, Any]]:
    """Closed live trades per strategy across every account's journal."""
    out: dict[str, dict[str, Any]] = {}
    for path in glob.glob(os.path.join(ROOT, "data", "users", "*", "journal.db")):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            rows = con.execute("SELECT strategy_name, net_pnl, entry_time FROM journal_entries "
                               "WHERE source = 'live' AND is_closed = 1").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for name, pnl, entry in rows:
            rec = out.setdefault(name, {"trades": 0, "wins": 0, "net_pnl": 0.0, "first": entry, "last": entry})
            rec["trades"] += 1
            rec["wins"] += pnl > 0
            rec["net_pnl"] += pnl
            rec["first"], rec["last"] = min(rec["first"], entry), max(rec["last"], entry)
    return out


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in runs if "error" not in r]
    if not ok:
        return {"stocks": 0}
    pnls = [p for r in ok for p in r["pnls"]]
    wins, losses = sum(p for p in pnls if p > 0), -sum(p for p in pnls if p < 0)
    rets, bh = [r["return_pct"] for r in ok], [r["buy_hold_pct"] for r in ok]
    oos_trades = sum(r["oos_trades"] for r in ok)
    oos_pnl = sum(r["oos_pnl"] for r in ok)
    folds = [sum(r["fold_pnl"][k] for r in ok) for k in range(FOLDS)]
    row = {
        "stocks": len(ok),
        "trades": len(pnls),
        "win_rate": round(sum(p > 0 for p in pnls) / len(pnls) * 100, 1) if pnls else None,
        "profit_factor": round(wins / losses, 2) if losses else None,
        "median_return_pct": round(statistics.median(rets), 2),
        "median_buy_hold_pct": round(statistics.median(bh), 2),
        "beat_buy_hold": sum(a > b for a, b in zip(rets, bh)),
        "profitable_stocks": sum(r > 0 for r in rets),
        "median_max_dd_pct": round(statistics.median(r["max_dd_pct"] for r in ok), 2),
        "oos_trades": oos_trades,
        "oos_return_pct": round(oos_pnl / (CAPITAL * len(ok)) * 100, 2),
        "fold_return_pct": [round(f / (CAPITAL * len(ok)) * 100, 2) for f in folds],
        "profitable_folds": sum(f > 0 for f in folds),
        "period": f"{min(r['start'] for r in ok)} → {max(r['end'] for r in ok)}",
        "per_stock": [{k: r[k] for k in ("symbol", "return_pct", "buy_hold_pct", "max_dd_pct", "oos_pnl", "oos_trades")}
                      | {"trades": len(r["pnls"])} for r in ok],
    }
    row["verdict"] = verdict(row)
    return row


def verdict(row: dict[str, Any]) -> str:
    """Plain-language grade from the pooled evidence (not from any single stock)."""
    if row["trades"] < 20:
        return "insufficient trades"
    pf, oos = row["profit_factor"] or 0, row["oos_return_pct"]
    steady = row["profitable_folds"] == FOLDS
    if pf >= 1.3 and oos > 0 and steady and row["oos_trades"] >= 10 and row["median_return_pct"] > row["median_buy_hold_pct"]:
        return "edge: profitable in every period and out of sample, beats buy-and-hold"
    if pf >= 1.3 and oos > 0 and steady:
        return "consistent: profitable in every period, behind buy-and-hold"
    if pf >= 1.1 and oos > 0:
        return "marginal: profitable but thin or behind buy-and-hold"
    if pf >= 1.0:
        return "break-even after costs"
    return "loses after costs"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stocks", type=int, default=8, help="stocks per timeframe (most history first)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--only", help="comma-separated strategy names")
    args = ap.parse_args()

    catalog = strategy_catalog()
    names = [n for n in catalog if not args.only or n in args.only.split(",")]
    universe = {tf: stocks_for(tf, args.stocks) for tf in WINDOW_DAYS}
    jobs = [(n, tf, tok, sym) for n in names for tf in catalog[n]["timeframes"] for tok, sym in universe[tf]]
    print(f"{len(names)} strategies · {len(jobs)} backtests · stocks per timeframe: "
          + ", ".join(f"{tf} {len(v)}" for tf, v in universe.items()), flush=True)

    t0 = time.time()
    results: dict[tuple[str, str], list[dict[str, Any]]] = {}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, *job): job for job in jobs}
        for i, fut in enumerate(as_completed(futures), 1):
            name, tf, _, sym = futures[fut]
            try:
                res = fut.result()
            except Exception as e:   # one failing backtest must not sink the report
                res = {"error": f"{type(e).__name__}: {e}"}
            results.setdefault((name, tf), []).append(res)
            if i % 25 == 0:
                print(f"  {i}/{len(jobs)} done ({time.time() - t0:.0f}s)", flush=True)

    live = live_record()
    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "settings": {"capital": CAPITAL, "risk_per_trade": RISK_PER_TRADE, "oos_fraction": OOS_FRACTION,
                           "folds": FOLDS,
                           "window_days": WINDOW_DAYS, "costs": "Zerodha-style (IndianCostModel)"},
              "live_trades_total": sum(v["trades"] for v in live.values()),
              "strategies": []}
    for name in names:
        meta = catalog[name]
        report["strategies"].append({
            "name": name, "label": meta["label"], "group": meta["group"], "description": meta["description"],
            "timeframes": {tf: aggregate(results.get((name, tf), [])) for tf in meta["timeframes"]},
            "live": live.get(name, {"trades": 0}),
        })
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, default=str)

    print(f"\n{'strategy':<26}{'tf':<10}{'stk':>4}{'trades':>7}{'win%':>6}{'PF':>6}{'med ret':>9}"
          f"{'med B&H':>9}{'beat':>6}{'OOS ret':>9}{'periods +':>10}{'live':>6}  verdict")
    for s in report["strategies"]:
        for tf, a in s["timeframes"].items():
            if not a.get("stocks"):
                print(f"{s['name']:<26}{tf:<10}   no data")
                continue
            pf = f"{a['profit_factor']:.2f}" if a["profit_factor"] is not None else "-"
            wr = f"{a['win_rate']:.0f}" if a["win_rate"] is not None else "-"
            print(f"{s['name']:<26}{tf:<10}{a['stocks']:>4}{a['trades']:>7}{wr:>6}{pf:>6}"
                  f"{a['median_return_pct']:>8.1f}%{a['median_buy_hold_pct']:>8.1f}%"
                  f"{a['beat_buy_hold']:>4}/{a['stocks']:<1}{a['oos_return_pct']:>8.2f}%{str(a['profitable_folds']) + '/' + str(FOLDS):>10}{s['live']['trades']:>6}  {a['verdict']}")
    print(f"\nlive trades in journals: {report['live_trades_total']} · {time.time() - t0:.0f}s · wrote {OUT}")


if __name__ == "__main__":
    main()
