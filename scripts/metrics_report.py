"""Business metrics report from data/analytics.db.

Run on the server from the app directory:
    cd /opt/verquoai/app
    sudo -u verquo /opt/verquoai/venv/bin/python scripts/metrics_report.py summary --days 7
    ... metrics_report.py features --days 7     most used features (API routes)
    ... metrics_report.py clicks --days 7       what people click / which tabs they open
    ... metrics_report.py users --days 30       activity per account and demo session
    ... metrics_report.py contacts              access requests (newest first)
    ... metrics_report.py contacts --csv > requests.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "analytics.db")

FUNNEL = ["demo_start", "otp_sent", "login_success", "login_failed", "account_linked", "contact_submitted",
          "step_up_verified", "demo_exit", "logout"]


def table(rows: list[tuple], headers: list[str]) -> None:
    rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))
    if not rows:
        print("(no data)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", choices=["summary", "features", "clicks", "users", "contacts"])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--csv", action="store_true", help="contacts: write CSV to stdout")
    args = parser.parse_args()
    if not os.path.exists(DB):
        sys.exit(f"No metrics yet ({DB} does not exist).")
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    since = (date.today() - timedelta(days=args.days - 1)).isoformat()

    if args.report == "summary":
        counts = ", ".join(f"SUM(event = '{e}') AS {e}" for e in FUNNEL)
        rows = conn.execute(f"""
            SELECT day,
                   COUNT(DISTINCT CASE WHEN event = 'page_view' AND device != 'bot' THEN visitor_id END) AS visitors,
                   SUM(event = 'page_view') AS page_views, {counts}
            FROM events WHERE day >= ? GROUP BY day ORDER BY day DESC""", (since,)).fetchall()
        table(rows, ["day", "visitors", "page_views", *FUNNEL])
        print()
        rows = conn.execute("""
            SELECT day, COUNT(DISTINCT CASE WHEN kind = 'user' THEN user_key END) AS active_users,
                   COUNT(DISTINCT CASE WHEN kind = 'demo' THEN user_key END) AS demo_sessions,
                   SUM(count) AS requests, SUM(CASE WHEN status = '5xx' THEN count ELSE 0 END) AS server_errors
            FROM request_counts WHERE day >= ? AND kind != 'bot' GROUP BY day ORDER BY day DESC""", (since,)).fetchall()
        table(rows, ["day", "active_users", "demo_sessions", "requests", "server_errors"])

    elif args.report == "features":
        rows = conn.execute("""
            SELECT route, method, kind, SUM(count) AS calls, COUNT(DISTINCT user_key) AS people,
                   ROUND(SUM(total_ms) / SUM(count), 1) AS avg_ms,
                   SUM(CASE WHEN status IN ('4xx', '5xx') THEN count ELSE 0 END) AS errors
            FROM request_counts WHERE day >= ? AND kind != 'bot'
            GROUP BY route, method, kind ORDER BY people DESC, calls DESC LIMIT 60""", (since,)).fetchall()
        table(rows, ["route", "method", "kind", "calls", "people", "avg_ms", "errors"])

    elif args.report == "clicks":
        rows = conn.execute("""
            SELECT event, page, target, COUNT(*) AS clicks, COUNT(DISTINCT visitor_id) AS people
            FROM events WHERE day >= ? AND event IN ('click', 'tab_view') AND device != 'bot'
            GROUP BY event, page, target ORDER BY people DESC, clicks DESC LIMIT 60""", (since,)).fetchall()
        table(rows, ["event", "page", "target", "clicks", "people"])

    elif args.report == "users":
        # usage data holds keyed hashes of Trading IDs: show the accounts this server knows
        sys.path.insert(0, ROOT)
        from src.auth.user_config import UserConfigService
        from src.metrics.store import metrics
        names = {metrics.user_ref(u["zerodha_id"]): u["zerodha_id"] for u in UserConfigService.get_instance().list_users()}
        rows = conn.execute("""
            SELECT user_key, kind, MIN(day) AS first_day, MAX(day) AS last_day, COUNT(DISTINCT day) AS days_active,
                   SUM(count) AS requests
            FROM request_counts WHERE day >= ? AND kind IN ('user', 'demo')
            GROUP BY user_key, kind ORDER BY last_day DESC, requests DESC LIMIT 100""", (since,)).fetchall()
        rows = [(names.get(r[0], r[0]),) + tuple(r[1:]) for r in rows]
        table(rows, ["user", "kind", "first_day", "last_day", "days_active", "requests"])

    elif args.report == "contacts":
        # read-only: a database the app has not migrated yet lacks the newer columns
        cols = {row[1] for row in conn.execute("PRAGMA table_info(contact_requests)")}
        newer = ", ".join(c if c in cols else f"'' AS {c}" for c in ("access_level", "platform"))
        rows = conn.execute(f"""
            SELECT id, ts, name, email, phone, trading_id, {newer}, source, emailed, status, message
            FROM contact_requests ORDER BY id DESC""").fetchall()
        headers = ["id", "time", "name", "email", "phone", "trading_id", "access", "platform", "source",
                   "emailed", "status", "message"]
        if args.csv:
            # Visitors typed these values: stop spreadsheets running them as formulas.
            def safe(v: object) -> object:
                return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
            writer = csv.writer(sys.stdout)
            writer.writerow(headers)
            writer.writerows([[safe(v) for v in r] for r in rows])
        else:
            table([r[:-1] + ((r[-1] or "").replace("\n", " ")[:60],) for r in rows], headers)


if __name__ == "__main__":
    main()
