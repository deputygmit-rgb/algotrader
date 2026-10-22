"""Day Trade board: when each signal was logged versus when its bar closed, and why
signals were skipped or not sent.

Reads the Executor agent's log (logs/agents/<ID>/executor-<date>.jsonl), where
the board journals every signal, status change and auto-trade decision.

    python scripts/board_signal_report.py WV1243            # today
    python scripts/board_signal_report.py WV1243 2026-10-22

A signal is detected once its bar's data arrives (normally 20-50 s after the
close). Larger lags mean the app was not running when the bar closed (stopped,
restarting, asleep: check `journalctl -u verquoai` for restarts) or the stock
joined the board later ("replay").
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
from collections import Counter
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNAL = re.compile(r"^Board signal: (\w+) (\S+) · (.+?) @ (\d\d:\d\d)")
ORDER = re.compile(r"^Board auto-trade (\w+): (\w+)(?: \d+)? (\S+) · (.+?)(?: — (.*)| → (.*))?$")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    account = re.sub(r"[^A-Za-z0-9-]", "", sys.argv[1]).upper()
    day = sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat()
    path = os.path.join(ROOT, "logs", "agents", account, f"executor-{day}.jsonl")
    if not os.path.exists(path):
        print(f"no log: {path}")
        return 1

    rows, orders = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts = datetime.fromisoformat(e["ts"])
            msg = e.get("message") or ""
            m = SIGNAL.match(msg)
            if m:
                side, sym, setup, close = m.groups()
                lag = (ts - ts.replace(hour=int(close[:2]), minute=int(close[3:]), second=0)).total_seconds()
                kind = "replay" if "replayed from before tracking" in msg else (
                    "skip: " + msg.split("not taken:")[1].strip() if "not taken:" in msg else "live")
                rows.append((ts, close, lag, kind, side, sym, setup))
                continue
            m = ORDER.match(msg)
            if m:
                orders.append((ts, m.group(1).lower(), m.group(3), m.group(4), (m.group(5) or m.group(6) or "").strip()))

    print(f"{account} · {day} · {len(rows)} board signals\n")
    print(f"{'logged':8}  {'bar':5}  {'lag':>8}  {'kind':22}  signal")
    for ts, close, lag, kind, side, sym, setup in rows:
        print(f"{ts:%H:%M:%S}  {close}  {lag / 60:6.1f}m  {kind:22}  {side} {sym} · {setup}")

    live = [r[2] for r in rows if not r[3].startswith("replay")]
    if live:
        on_time = [x for x in live if x <= 120]
        print(f"\nlive signals: {len(live)} · detected within 2 min: {len(on_time)}"
              + (f" · median {statistics.median(on_time):.0f}s" if on_time else "")
              + (f" · late (> 2 min): {len(live) - len(on_time)}" if len(on_time) < len(live) else ""))
    kinds = Counter(r[3] for r in rows)
    print("by kind: " + ", ".join(f"{k} {n}" for k, n in kinds.most_common()))
    if orders:
        print("\nauto-trade decisions:")
        for ts, decision, sym, setup, why in orders:
            print(f"  {ts:%H:%M:%S}  {decision:8} {sym} · {setup}{' — ' + why if why else ''}")
        print("reasons: " + ", ".join(f"{k or decision} {n}" for (decision, k), n in
                                      Counter((d, w) for _, d, _, _, w in orders).most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
