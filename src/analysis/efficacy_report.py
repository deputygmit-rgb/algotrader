"""The daily strategy review email: each setup's track record and what changed.

Built after the nightly rebuild (``scripts/rebuild_efficacy.py --email``) from
the new record, yesterday's record and today's Day Trade board ledger (the
``signals`` table). Plain text with an HTML twin; it names no broker.
"""
from __future__ import annotations

import html
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Optional

from src.analysis.efficacy import MAX_BONUS, MAX_PENALTY, MIN_TRADES, RESUME_ABOVE, SUSPEND_BELOW, WINDOW_DAYS, WINDOW_MAX_POINTS, WINDOW_TRADES

BOARD = {"day": "DAY TRADE (5-minute setups)", "swing": "SWING (1-hour setups, daily context)"}


def changes(records: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]]) -> list[str]:
    """Plain sentences for what moved since the last review."""
    out = []
    for key, r in sorted(records.items()):
        name = f"{r['kind'].upper()} {r['setup']} {r['side']}"
        before = previous.get(key)
        if before is None:
            out.append(f"{name}: first record - {r['avg_r']:+.2f}R a trade over {r['n']}, quality {r['adj']:+d}")
            continue
        if before.get("status") != r["status"]:
            what = "SUSPENDED" if r["status"] == "suspended" else "REINSTATED"
            out.append(f"{name}: {what} - {r['avg_r']:+.2f}R a trade over its last {r['n']} (was {before.get('avg_r', 0):+.2f}R)")
        elif abs(r["adj"] - before.get("adj", 0)) >= 3:
            out.append(f"{name}: quality {before.get('adj', 0):+d} -> {r['adj']:+d} ({before.get('avg_r', 0):+.2f}R -> {r['avg_r']:+.2f}R)")
    for key in sorted(set(previous) - set(records)):
        p = previous[key]
        out.append(f"{p['kind'].upper()} {p['setup']} {p['side']}: no trades left in the {WINDOW_DAYS}-day window - no adjustment")
    return out


def board_today(db_path: str, today: date) -> dict[str, dict[str, Any]]:
    """Today's Day Trade board ledger per account: signals, taken, skips, closed and net R."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute("SELECT account, status, result_r, skipped, backfilled FROM signals "
                           "WHERE source='board' AND session=?", (today.isoformat(),)).fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for account, status, result_r, skipped, backfilled in rows:
        if backfilled:
            continue
        a = out.setdefault(account, {"signals": 0, "taken": 0, "closed": 0, "net_r": 0.0, "wins": 0, "skips": Counter()})
        a["signals"] += 1
        if skipped:
            a["skips"][skipped.split(":")[0]] += 1
            continue
        a["taken"] += 1
        if result_r is not None:
            a["closed"] += 1
            a["net_r"] += float(result_r)
            a["wins"] += float(result_r) > 0
    return out


def _table(rows: list[dict[str, Any]]) -> list[list[str]]:
    return [[r["setup"], r["side"], str(r["n"]), f"{r['avg_r']:+.3f}", f"{r['shrunk_r']:+.3f}", f"{r['win_rate']}%",
             f"{r['adj']:+d}", r["status"].upper()] for r in rows]


HEAD = ["SETUP", "SIDE", "TRADES", "AVG R", "SHRUNK R", "WIN", "QUALITY", "STATUS"]


def oi_lines(oi_bias: Optional[dict[str, Any]]) -> list[str]:
    from src.analysis.market_regime import MARKET_WEIGHT

    out = []
    for u, r in sorted((oi_bias or {}).items()):
        if not r or not r.get("n"):
            out.append(f"  {u}: no readings yet - half weight ({MARKET_WEIGHT * 0.5:.1f} of {MARKET_WEIGHT} points)")
            continue
        out.append(f"  {u}: {r['status'].upper()} - {r['n']} directional readings, next {r.get('horizon_min', 30)} min called "
                   f"{r['hit_rate']:.0%} vs {r['chance']:.0%} by chance (edge {r['edge']:+.1%}); weight "
                   f"{MARKET_WEIGHT * r['factor']:.1f} of {MARKET_WEIGHT} quality points")
    return out


def window_lines(windows: Optional[dict[str, Any]]) -> list[str]:
    from src.analysis.efficacy import WINDOW_LABEL

    rows = sorted((windows or {}).values(), key=lambda r: (r["setup"], r["side"], r["window"]))
    return [f"  {r['setup']} {r['side']} {WINDOW_LABEL.get(r['window'], r['window'])}: {r['avg_r']:+.2f}R a trade "
            f"over {r['n']} ({r['win_rate']}% won), quality {r['adj']:+d}" for r in rows]


def build(records: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]], today: date,
          board: dict[str, dict[str, Any]], note: str = "",
          oi_bias: Optional[dict[str, Any]] = None,
          windows: Optional[dict[str, Any]] = None) -> tuple[str, str, str]:
    """(subject, text, html) of the review."""
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records.values():
        by_kind[r["kind"]].append(r)
    for rows in by_kind.values():
        rows.sort(key=lambda r: (r["setup"], r["side"]))
    suspended = [r for r in records.values() if r["status"] == "suspended"]
    moves = changes(records, previous)
    subject = (f"VerquoAI strategy review {today.strftime('%d %b %Y')} - "
               f"{len(suspended)} suspended, {len(moves)} change{'s' if len(moves) != 1 else ''}")

    rules = (f"Each setup and side keeps its last {WINDOW_TRADES} closed trades within {WINDOW_DAYS} days "
             f"(net R after costs). Quality moves {-MAX_PENALTY}..+{MAX_BONUS} points with the expectancy shrunk "
             f"towards zero; a setup below {SUSPEND_BELOW:+.2f}R over {MIN_TRADES}+ trades is suspended (its "
             f"signals are shown and followed but not taken, auto-trade never sends them) until it recovers above "
             f"{RESUME_ABOVE:+.2f}R. Signals keep the verdict they were first shown with.")

    lines = [f"VerquoAI - strategy review, {today.strftime('%A %d %B %Y')}", ""]
    lines += ["WHAT CHANGED"] + ([f"  - {m}" for m in moves] or ["  - nothing: every setup kept its status and adjustment"]) + [""]
    if suspended:
        lines += ["SUSPENDED NOW (signals shown, not taken)"] + [
            f"  - {r['kind'].upper()} {r['setup']} {r['side']}: {r['avg_r']:+.2f}R a trade over {r['n']}, {r['win_rate']}% won"
            for r in sorted(suspended, key=lambda r: r["avg_r"])] + [""]
    for kind in ("day", "swing"):
        rows = by_kind.get(kind, [])
        lines.append(BOARD[kind])
        if not rows:
            lines += ["  no closed trades in the window yet - signals are not adjusted", ""]
            continue
        table = [HEAD] + _table(rows)
        widths = [max(len(row[i]) for row in table) for i in range(len(HEAD))]
        lines += ["  " + "  ".join(c.ljust(w) for c, w in zip(row, widths)) for row in table] + [""]
    lines += ["INDEX OPTIONS-OI BIAS (moves Day Trade quality by up to the weight shown)"] + (
        oi_lines(oi_bias) or ["  no record yet"]) + [""]
    lines += [f"ENTRY WINDOW (live Day Trade board trades; moves quality by up to {WINDOW_MAX_POINTS} points)"] + (
        window_lines(windows) or ["  no closed live board trades yet"]) + [""]
    lines.append("TODAY ON THE DAY TRADE BOARDS (live signals, replays excluded)")
    if board:
        for account, a in sorted(board.items()):
            skips = ", ".join(f"{k} {n}" for k, n in a["skips"].most_common()) or "none"
            win = f", {a['wins']}/{a['closed']} won" if a["closed"] else ""
            lines.append(f"  {account}: {a['signals']} signals, {a['taken']} taken, {a['closed']} closed "
                         f"{a['net_r']:+.2f}R net{win}; skipped: {skips}")
    else:
        lines.append("  no live board signals today")
    lines += ["", "HOW THE RECORD WORKS", "  " + rules,
              "  The record replays the stored bars through the same rules the boards use, without each board's "
              "stock selection, so it measures the setups themselves. Past results do not promise future ones.",
              ""]
    if note:
        lines.append(f"({note})")
    text = "\n".join(lines)

    e = html.escape

    def html_table(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "<p style='color:#666'>No closed trades in the window yet - signals are not adjusted.</p>"
        head = "".join(f"<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ccc'>{h}</th>" for h in HEAD)
        body = ""
        for r, cells in zip(rows, _table(rows)):
            color = "#c0392b" if r["status"] == "suspended" else "#1e8449" if r["adj"] > 0 else "#b9770e" if r["adj"] < 0 else "#333"
            body += "<tr>" + "".join(f"<td style='padding:3px 8px;color:{color}'>{e(c)}</td>" for c in cells) + "</tr>"
        return f"<table style='border-collapse:collapse;font:13px monospace'><tr>{head}</tr>{body}</table>"

    parts = [f"<h2 style='font-family:sans-serif'>Strategy review - {e(today.strftime('%d %b %Y'))}</h2>",
             "<h3 style='font-family:sans-serif'>What changed</h3><ul>"
             + ("".join(f"<li>{e(m)}</li>" for m in moves) or "<li>Nothing: every setup kept its status and adjustment.</li>")
             + "</ul>"]
    for kind in ("day", "swing"):
        parts.append(f"<h3 style='font-family:sans-serif'>{e(BOARD[kind])}</h3>{html_table(by_kind.get(kind, []))}")
    board_items = "".join(
        f"<li>{e(acc)}: {a['signals']} signals, {a['taken']} taken, {a['closed']} closed {a['net_r']:+.2f}R net; "
        f"skipped: {e(', '.join(f'{k} {n}' for k, n in a['skips'].most_common()) or 'none')}</li>"
        for acc, a in sorted(board.items())) or "<li>No live board signals today.</li>"
    parts.append("<h3 style='font-family:sans-serif'>Index options-OI bias</h3><ul>"
                 + "".join(f"<li>{e(x.strip())}</li>" for x in oi_lines(oi_bias)) + "</ul>")
    parts.append("<h3 style='font-family:sans-serif'>Entry window (live Day Trade board trades)</h3><ul>"
                 + ("".join(f"<li>{e(x.strip())}</li>" for x in window_lines(windows))
                    or "<li>No closed live board trades yet.</li>") + "</ul>")
    parts.append(f"<h3 style='font-family:sans-serif'>Today on the Day Trade boards</h3><ul>{board_items}</ul>")
    parts.append(f"<p style='font:12px sans-serif;color:#555'>{e(rules)} The record replays the stored bars through "
                 f"the same rules the boards use; past results do not promise future ones.{(' ' + e(note)) if note else ''}</p>")
    return subject, text, "<div style='font-family:sans-serif'>" + "".join(parts) + "</div>"


def recipients(env: dict[str, str]) -> list[str]:
    raw = env.get("REPORT_EMAILS") or env.get("ADMIN_EMAIL") or ""
    return [a.strip() for a in raw.split(",") if "@" in a and "\n" not in a and "\r" not in a][:5]


def should_send(previous_as_of: Optional[str], today: date, force: bool) -> bool:
    """At most once a day: the first rebuild of a new day (or when forced)."""
    return force or previous_as_of != today.isoformat()
