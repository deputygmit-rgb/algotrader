"""Rebuild the setups' rolling track record (data/efficacy.json).

Replays the stored bars in ``data/market_data.db`` through the engines the
boards use - 5-minute bars through the Day Trade setups, 1-hour + daily bars
through the Swing setups - and keeps, per (board, setup, side), the last
closed trades before today (``src/analysis/efficacy.py`` for the rules).

The web app runs this once a day after the close as a separate low-priority
process (it reads a lot of history); run it by hand any time:

    python scripts/rebuild_efficacy.py
    python scripts/rebuild_efficacy.py --show          # print the current record
    python scripts/rebuild_efficacy.py --email         # and email the review (first rebuild of the day)
    python scripts/rebuild_efficacy.py --preview       # print the review instead of sending it

The review goes to REPORT_EMAILS (comma-separated) or else ADMIN_EMAIL, with
the same SMTP_* settings as the sign-in email. It carries the full strategy
performance report as a Markdown attachment (``src/analysis/performance_report.py``,
also saved to ``data/reports/performance-<date>.md``); the replayed trades are
kept in ``data/reports/setup_trades.json`` for the admin panel's performance view.
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

from src.analysis.efficacy import PATH, WINDOW_DAYS, oi_bias_record, save, summarize, window_records  # noqa: E402

DAY_HISTORY_DAYS = WINDOW_DAYS + 45        # warm-up: the setups need 15 earlier sessions
TRADE_FIELDS = ("quality", "status", "gross_r", "cost_r", "result_r", "mfe_r", "mae_r", "bars_held", "time", "exit_time")
REPORTS_KEPT = 60


def _trade(kind: str, symbol: str, s: dict, when: str) -> dict:
    """One closed trade with what the performance report needs (rounded, compact)."""
    t = {"kind": kind, "symbol": symbol, "setup": s["setup"], "side": s["side"], "date": when}
    for k in TRADE_FIELDS:
        v = s.get(k)
        t[k] = round(v, 3) if isinstance(v, float) else v
    return t


def symbols(con: sqlite3.Connection) -> dict[int, str]:
    try:
        return {r[0]: r[1] for r in con.execute("SELECT instrument_token, tradingsymbol FROM instruments")}
    except sqlite3.Error:
        return {}
SWING_DAILY_DAYS = WINDOW_DAYS + 120       # daily EMA 50 + 20-day levels before the window


def day_trades(con: sqlite3.Connection, today: date, names: dict[int, str] | None = None) -> list[dict]:
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
                out.append(_trade("day", (names or {}).get(token, str(token)), s, s["date"]))
    return out


def board_trades(con: sqlite3.Connection, today: date) -> list[dict]:
    """Closed live Day Trade board signals (one per stock, setup and bar across accounts),
    for the entry-window record: unlike the replay they went through the board's money-flow filter."""
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    try:
        rows = con.execute("SELECT session, symbol, setup, side, signal_time, result_r FROM signals "
                           "WHERE source='board' AND result_r IS NOT NULL AND session >= ? AND session < ?",
                           (since, today.isoformat())).fetchall()
    except sqlite3.Error:
        return []
    seen: dict[tuple, dict] = {}
    for session, symbol, setup, side, t, r in rows:
        seen.setdefault((session, symbol, setup, side, t),
                        {"date": session, "setup": setup, "side": side, "time": t, "result_r": r})
    return list(seen.values())


def swing_trades(con: sqlite3.Connection, today: date, names: dict[int, str] | None = None) -> list[dict]:
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
                out.append(_trade("swing", (names or {}).get(token, str(token)), s, s["exit_time"][:10]))
    return out


def load_env_file(path: str = os.path.join(ROOT, ".env")) -> None:
    """Read KEY=VALUE lines from the app's .env into the environment, never
    overriding what is already set: the service gets .env from systemd, a run
    from a shell (sudo -u verquo python scripts/...) would otherwise lack the
    mail settings."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().removeprefix("export ").strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def main() -> None:
    load_env_file()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "market_data.db"))
    ap.add_argument("--out", default=PATH)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--email", action="store_true", help="email the review (once a day)")
    ap.add_argument("--force-email", action="store_true", help="email the review even if already sent today")
    ap.add_argument("--preview", action="store_true", help="print the review text")
    args = ap.parse_args()
    if args.show:
        if not os.path.exists(args.out):
            print(f"no track record yet ({args.out}): the web app builds it 2 minutes after it starts "
                  f"and nightly after the close - or run this script without --show to build it now")
            return
        with open(args.out, encoding="utf-8") as f:
            data = json.load(f)
        print(f"as of {data['as_of']}")
        for u, r in (data.get("oi_bias") or {}).items():
            print(f"  OI bias {u}: {r.get('status')} - {r.get('n', 0)} directional readings - hit {r.get('hit_rate')} "
                  f"vs chance {r.get('chance')} - weight x{r.get('factor')}")
        for r in sorted(data["records"].values(), key=lambda r: (r["kind"], r["setup"], r["side"])):
            print(f"  {r['kind']:5} {r['setup']:15} {r['side']:5} n={r['n']:3d} avg {r['avg_r']:+.3f}R "
                  f"shrunk {r['shrunk_r']:+.3f}R win {r['win_rate']:3d}%  adj {r['adj']:+d}  {r['status']}")
        return
    started = time.time()
    today = date.today()
    previous, previous_as_of = {}, None
    try:
        with open(args.out, encoding="utf-8") as f:
            old = json.load(f)
        previous, previous_as_of = old.get("records") or {}, old.get("as_of")
    except (OSError, ValueError):
        pass
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    names = symbols(con)
    trades = day_trades(con, today, names) + swing_trades(con, today, names)
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    oi_bias = {}
    for u in ("NIFTY", "SENSEX"):
        try:
            rows = con.execute("SELECT ts, bias, spot_price FROM oi_bias_history WHERE underlying=? AND ts >= ? "
                               "AND ts < ? ORDER BY ts", (u, since, today.isoformat())).fetchall()
        except sqlite3.Error:
            rows = []
        oi_bias[u] = oi_bias_record(rows)
    windows = window_records(board_trades(con, today), today)
    con.close()
    records = summarize(trades, today, previous)
    note = f"{len(trades)} trades replayed in {time.time() - started:.0f}s"
    save(records, today, args.out, note=note, oi_bias=oi_bias, windows=windows)
    print(f"efficacy: {len(records)} setup records from {len(trades)} trades in {time.time() - started:.0f}s")

    # the full performance report: trades kept for the admin panel, Markdown kept and mailed
    from src.analysis import performance_report as perf

    perf.save_trades(trades, today)
    full = perf.build(trades, today, days=WINDOW_DAYS,
                      ledger=perf.live_ledger(args.db, (today - timedelta(days=WINDOW_DAYS)).isoformat()),
                      efficacy=records, as_of=today.isoformat())
    md = perf.markdown(full)
    md_path = os.path.join(perf.REPORT_DIR, f"performance-{today.isoformat()}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    old_reports = sorted(n for n in os.listdir(perf.REPORT_DIR) if n.startswith("performance-") and n.endswith(".md"))
    for name in old_reports[:-REPORTS_KEPT]:
        os.remove(os.path.join(perf.REPORT_DIR, name))
    print(f"performance report: {md_path}")

    if args.email or args.force_email or args.preview:
        from src.analysis import efficacy_report as report

        subject, text, html = report.build(records, previous, today, report.board_today(args.db, today), note,
                                           oi_bias=oi_bias, windows=windows)
        text += "\n\nThe full strategy performance report (per setup, per day, parameters) is attached as Markdown.\n"
        if args.preview:
            print(f"\nSubject: {subject}\n\n{text}\n\n{md}")
        elif report.should_send(previous_as_of, today, args.force_email):
            from src.utils.mailer import send_mail

            to = report.recipients(dict(os.environ))
            attachment = [(f"verquoai-performance-{today.isoformat()}.md", md, "markdown")]
            sent = [addr for addr in to if send_mail(addr, subject, text, html=html, attachments=attachment)]
            print(f"review emailed to {len(sent)} of {len(to)} recipient(s)" if to else "review not emailed: set ADMIN_EMAIL or REPORT_EMAILS")


if __name__ == "__main__":
    main()
