"""Mask Telegram bot tokens already written to the logs (logging now redacts them as they are written).

Run on the server from the app directory (stop nothing; rewrites in place):
    cd /opt/verquoai/app
    sudo -u verquo /opt/verquoai/venv/bin/python scripts/redact_logs.py

Covers logs/*.log* and the event log database (data/events.db). Prints counts, never the tokens.
"""
from __future__ import annotations

import glob
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.utils.logger import _BOT_TOKEN  # noqa: E402


def redact_files() -> int:
    total = 0
    for path in glob.glob(os.path.join(ROOT, "logs", "**", "*.log*"), recursive=True):
        try:
            with open(path, encoding="utf-8", errors="surrogateescape") as f:
                text = f.read()
        except OSError:
            continue
        cleaned, n = _BOT_TOKEN.subn("[redacted]", text)
        if n:
            with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(cleaned)
            print(f"  {os.path.relpath(path, ROOT)}: {n}")
            total += n
    return total


def redact_db(path: str) -> int:
    if not os.path.exists(path):
        return 0
    conn = sqlite3.connect(path)
    conn.create_function("vq_redact", 1, lambda v: _BOT_TOKEN.sub("[redacted]", v) if isinstance(v, str) else v)
    conn.create_function("vq_has_token", 1, lambda v: int(isinstance(v, str) and bool(_BOT_TOKEN.search(v))))
    total = 0
    try:
        with conn:
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
                for _, column, kind, *_ in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
                    if kind.upper() not in ("TEXT", ""):
                        continue
                    n = conn.execute(f'UPDATE "{table}" SET "{column}" = vq_redact("{column}") '
                                     f'WHERE vq_has_token("{column}")').rowcount
                    if n:
                        print(f"  {os.path.relpath(path, ROOT)} {table}.{column}: {n}")
                        total += n
    finally:
        conn.close()
    return total


if __name__ == "__main__":
    found = redact_files() + redact_db(os.path.join(ROOT, "data", "events.db"))
    print(f"Redacted {found} occurrence(s)." if found else "No bot tokens found.")
