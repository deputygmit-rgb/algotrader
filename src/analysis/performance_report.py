"""Strategy performance report for the Day Trade and Swing boards.

One report, two outputs: Markdown (emailed nightly, downloadable from the admin
panel) and structured data (the admin panel's performance view).

Sources
* **Replay** - every setup run over the stored bars of the whole universe by the
  engines the boards use (``scripts/rebuild_efficacy.py`` saves the closed trades
  to ``data/reports/setup_trades.json`` each night). Net of slippage and charges.
  This is the statistically meaningful sample: every stock, every session.
* **Live board** - the signals the boards actually showed (the ``signals``
  ledger): what was taken, skipped by the portfolio cap or a suspended setup, and
  how it ended.

Per setup and side: sample size, win rate, expectancy (mean R) with a 95%
confidence interval, median, profit factor, payoff ratio, SQN, total R, maximum
drawdown and longest losing streak in R, average favourable / adverse excursion,
how much of the favourable move the exits keep, cost drag, holding time, the exit
mix, the last 20 trades against the whole window, a daily Sharpe / Sortino, and a
verdict. Per day: trades, win rate, net and cumulative R, best and worst setup.
Each setup's parameters are listed as the engine runs them today.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any, Iterable, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORT_DIR = os.path.join(ROOT, "data", "reports")
TRADES_PATH = os.path.join(REPORT_DIR, "setup_trades.json")
BOARDS = {"day": "Day Trade · 5-minute setups", "swing": "Swing · 1-hour setups with daily context"}
RECENT_TRADES = 20
MIN_SAMPLE = 30           # below this a verdict is "too few trades"
DAYS_IN_REPORT = 15       # day-by-day rows per board in the Markdown


# ── statistics ───────────────────────────────────────────────────────────
def _round(x: Optional[float], d: int = 2) -> Optional[float]:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, d)


def _max_drawdown(rs: list[float]) -> float:
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def _longest_losing_streak(rs: list[float]) -> int:
    best = run = 0
    for r in rs:
        run = run + 1 if r <= 0 else 0
        best = max(best, run)
    return best


def stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Performance of closed trades (``result_r`` net of costs), in trade order."""
    trades = sorted(trades, key=lambda t: (str(t.get("date")), str(t.get("time") or "")))
    rs = [float(t["result_r"]) for t in trades if t.get("result_r") is not None]
    n = len(rs)
    if not n:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    gross_win, gross_loss = sum(wins), -sum(losses)
    mfe = [float(t["mfe_r"]) for t in trades if t.get("mfe_r") is not None]
    mae = [float(t["mae_r"]) for t in trades if t.get("mae_r") is not None]
    costs = [float(t["cost_r"]) for t in trades if t.get("cost_r") is not None]
    grosses = [float(t["gross_r"]) for t in trades if t.get("gross_r") is not None]
    holds = [float(t["bars_held"]) for t in trades if t.get("bars_held") is not None]
    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.get("result_r") is not None:
            daily[str(t["date"])] += float(t["result_r"])
    dvals = list(daily.values())
    dmean = sum(dvals) / len(dvals) if dvals else 0.0
    dsd = math.sqrt(sum((v - dmean) ** 2 for v in dvals) / (len(dvals) - 1)) if len(dvals) > 1 else 0.0
    down = [min(v, 0.0) for v in dvals]
    dsd_down = math.sqrt(sum(v * v for v in down) / len(down)) if down else 0.0
    recent = rs[-RECENT_TRADES:]
    t_stat = mean / se if se else 0.0
    avg_mfe = sum(mfe) / len(mfe) if mfe else None
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1),
        "expectancy_r": _round(mean, 3), "median_r": _round(median(rs), 3), "total_r": _round(sum(rs), 2),
        "ci95": [_round(mean - 1.96 * se, 3), _round(mean + 1.96 * se, 3)] if n > 1 else [None, None],
        "t_stat": _round(t_stat, 2),
        "profit_factor": _round(gross_win / gross_loss, 2) if gross_loss else None,
        "payoff": _round((gross_win / len(wins)) / (gross_loss / len(losses)), 2) if wins and losses and gross_loss else None,
        "sqn": _round(math.sqrt(min(n, 100)) * mean / sd, 2) if sd else None,
        "std_r": _round(sd, 3),
        "max_dd_r": _round(_max_drawdown(rs), 2),
        "losing_streak": _longest_losing_streak(rs),
        "best_r": _round(max(rs), 2), "worst_r": _round(min(rs), 2),
        "avg_mfe_r": _round(avg_mfe, 2), "avg_mae_r": _round(sum(mae) / len(mae), 2) if mae else None,
        # share of the average favourable move the exits keep (exit efficiency)
        "capture": _round(mean / avg_mfe * 100, 0) if avg_mfe and avg_mfe > 0 else None,
        "avg_cost_r": _round(sum(costs) / len(costs), 3) if costs else None,
        "cost_drag": _round(sum(costs) / sum(abs(g) for g in grosses) * 100, 1) if costs and grosses and any(grosses) else None,
        "avg_hold_bars": _round(sum(holds) / len(holds), 1) if holds else None,
        "exits": dict(Counter(str(t.get("status") or "?") for t in trades if t.get("result_r") is not None)),
        "recent_n": len(recent), "recent_r": _round(sum(recent) / len(recent), 3),
        "days": len(dvals), "sharpe_daily": _round(dmean / dsd * math.sqrt(252), 2) if dsd else None,
        "sortino_daily": _round(dmean / dsd_down * math.sqrt(252), 2) if dsd_down else None,
        "first": str(trades[0].get("date")), "last": str(trades[-1].get("date")),
    }


def verdict(s: dict[str, Any], efficacy: Optional[dict[str, Any]] = None) -> str:
    """One word on the evidence, not on hope."""
    if efficacy and efficacy.get("status") == "suspended":
        return "suspended"
    n = s.get("n", 0)
    if n < MIN_SAMPLE:
        return "too few trades"
    lo, hi = s["ci95"]
    if lo is not None and lo > 0:
        return "proven edge"
    if hi is not None and hi < 0:
        return "losing"
    if s["expectancy_r"] > 0:
        trend = s["recent_r"] is not None and s["recent_r"] < s["expectancy_r"] - 0.15
        return "positive, fading" if trend else "positive, unproven"
    return "no edge"


# ── parameters as the engines run them ──────────────────────────────────
def parameters() -> dict[str, list[dict[str, Any]]]:
    from src.analysis import intraday_setups as day
    from src.analysis import swing_setups as swing
    from src.api.setup_rules import DAY_SETUP_RULES, SWING_SETUP_RULES

    common_day = {
        "quality_min": day.MIN_QUALITY, "time_of_day_volume_min": f"{day.TOD_MIN_X}x ({day.TOD_SESSIONS}-session median)",
        "risk": f"{day.MIN_RISK_ATR}-{day.MAX_RISK_ATR} daily ATR", "targets": f"T1 {day.TARGET1_R}R (half) -> breakeven, T2 {day.TARGET2_R}R",
        "time_exit": f"{day.MAX_HOLD_BARS * day.BAR_MINUTES // 60} h or {day.LAST_EXIT_TIME}", "last_entry": day.LAST_ENTRY_TIME,
        "per_stock_day": f"{day.MAX_TRADES_PER_DAY} trades, {day.COOLDOWN_BARS * day.BAR_MINUTES} min cooldown",
        "costs": f"slippage {day.SLIPPAGE_PCT * 100:.2f}% + charges on Rs {day.REFERENCE_NOTIONAL:,.0f}",
    }
    common_swing = {
        "quality_min": swing.MIN_QUALITY, "risk": f"{swing.MIN_RISK_ATR}-{swing.MAX_RISK_ATR} daily ATR",
        "targets": f"T1 {swing.TARGET1_R}R (half) -> breakeven, T2 {swing.TARGET2_R}R",
        "time_exit": f"{swing.MAX_HOLD_BARS // swing.BARS_PER_SESSION} sessions",
        "cooldown": f"{swing.COOLDOWN_BARS // swing.BARS_PER_SESSION} session",
        "costs": f"slippage {swing.SLIPPAGE_PCT * 100:.2f}% + charges on Rs {swing.REFERENCE_NOTIONAL:,.0f}",
    }
    out = {"day": [], "swing": []}
    for setup, rule in DAY_SETUP_RULES.items():
        out["day"].append({"setup": setup, "label": day.SETUP_LABEL.get(setup, setup),
                           "enabled": setup in day.DEFAULT_SETUPS, "volume_min": f"{rule['vol']}x 10-bar avg" if rule["vol"] else "-",
                           "long": rule["long"], "short": rule["short"], "stop": rule["stop"], **common_day})
    for setup, rule in SWING_SETUP_RULES.items():
        out["swing"].append({"setup": setup, "label": swing.SETUP_LABEL.get(setup, setup), "enabled": True,
                             "volume_min": f"{rule['vol']}x (previous session and hour normal)",
                             "long": rule["long"], "short": rule["short"], "stop": rule["stop"], **common_swing})
    return out


# ── data ─────────────────────────────────────────────────────────────────
def save_trades(trades: list[dict[str, Any]], as_of: date, path: str = TRADES_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"as_of": as_of.isoformat(), "trades": trades}, f, separators=(",", ":"))
    os.replace(tmp, path)


def load_trades(path: str = TRADES_PATH) -> tuple[Optional[str], list[dict[str, Any]]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("as_of"), data.get("trades") or []
    except (OSError, ValueError):
        return None, []


def live_ledger(db_path: str, since: str) -> list[dict[str, Any]]:
    """The boards' signals since ``since`` from the ledger (read-only)."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT source, account, strategy, symbol, session, signal_time, side, setup, quality, status, result_r, "
            "gross_r, cost_r, mfe_r, mae_r, bars_held, backfilled, skipped, decision FROM signals "
            "WHERE source IN ('board', 'swing') AND session >= ?", (since,))]
        con.close()
        return rows
    except sqlite3.Error:
        return []


def _efficacy() -> dict[str, dict[str, Any]]:
    from src.analysis.efficacy import PATH

    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f).get("records") or {}
    except (OSError, ValueError):
        return {}


def build(trades: Iterable[dict[str, Any]], today: date, days: int = 60, ledger: Optional[list[dict[str, Any]]] = None,
          efficacy: Optional[dict[str, dict[str, Any]]] = None, as_of: Optional[str] = None) -> dict[str, Any]:
    """The whole report as data: per board, overall / per setup-side / per day, live board, parameters."""
    since = (today - timedelta(days=days)).isoformat()
    efficacy = _efficacy() if efficacy is None else efficacy
    params = parameters()
    window = [t for t in trades if since <= str(t.get("date")) < today.isoformat() and t.get("result_r") is not None]
    report: dict[str, Any] = {"today": today.isoformat(), "since": since, "days": days, "as_of": as_of, "boards": {}}
    for kind, title in BOARDS.items():
        ts = [t for t in window if t.get("kind") == kind]
        by_setup: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in ts:
            by_setup[(t["setup"], t["side"])].append(t)
            by_day[str(t["date"])].append(t)
        setups = []
        for (setup, side), group in sorted(by_setup.items()):
            s = stats(group)
            eff = efficacy.get(f"{kind}|{setup}|{side}")
            setups.append({"setup": setup, "side": side, **s, "verdict": verdict(s, eff),
                           "efficacy": {k: eff.get(k) for k in ("adj", "shrunk_r", "status")} if eff else None})
        cum = 0.0
        day_rows = []
        for d in sorted(by_day):
            group = by_day[d]
            net = sum(float(t["result_r"]) for t in group)
            cum += net
            per = defaultdict(float)
            for t in group:
                per[f"{t['setup']} {t['side']}"] += float(t["result_r"])
            best = max(per.items(), key=lambda kv: kv[1])
            worst = min(per.items(), key=lambda kv: kv[1])
            day_rows.append({"date": d, "trades": len(group), "wins": sum(1 for t in group if float(t["result_r"]) > 0),
                             "net_r": round(net, 2), "cum_r": round(cum, 2), "best": [best[0], round(best[1], 2)],
                             "worst": [worst[0], round(worst[1], 2)],
                             "setups": {k: round(v, 2) for k, v in per.items()}})
        live = _live_days(kind, ledger or [])
        report["boards"][kind] = {"title": title, "overall": stats(ts), "setups": setups, "daily": day_rows,
                                  "live": live, "parameters": params[kind]}
    return report


def _live_days(kind: str, ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source = "board" if kind == "day" else "swing"
    days: dict[str, dict[str, Any]] = {}
    for r in ledger:
        if r.get("source") != source or r.get("backfilled"):
            continue
        d = days.setdefault(r["session"], {"date": r["session"], "signals": 0, "taken": 0, "skipped": 0, "closed": 0,
                                           "wins": 0, "net_r": 0.0, "orders": 0, "accounts": set()})
        d["signals"] += 1
        d["accounts"].add(r.get("account") or "")
        if r.get("decision") == "sent":
            d["orders"] += 1
        if r.get("skipped"):
            d["skipped"] += 1
            continue
        d["taken"] += 1
        if r.get("result_r") is not None:
            d["closed"] += 1
            d["net_r"] += float(r["result_r"])
            d["wins"] += float(r["result_r"]) > 0
    out = []
    for d in sorted(days.values(), key=lambda x: x["date"]):
        d["accounts"] = len(d["accounts"])
        d["net_r"] = round(d["net_r"], 2)
        out.append(d)
    return out


# ── Markdown ─────────────────────────────────────────────────────────────
def _f(x: Any, fmt: str = "{:+.2f}", empty: str = "–") -> str:
    return empty if x is None else fmt.format(x)


def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    def esc(cell: Any) -> str:
        return str(cell).replace("|", "\\|").replace("\n", " ")
    return (["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
            + ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows])


def markdown(report: dict[str, Any]) -> str:
    lines = [f"# VerquoAI strategy performance — {report['today']}", "",
             f"Window: **{report['since']} to {report['today']}** ({report['days']} days). Replay of the stored bars "
             f"of the whole universe through the boards' engines, net of slippage and charges"
             + (f" (trades replayed {report['as_of']})" if report.get("as_of") else "") + ". R = the trade's initial risk.", ""]
    lines += ["## Summary", ""]
    rows = []
    for kind, b in report["boards"].items():
        o = b["overall"]
        if not o.get("n"):
            rows.append([b["title"], "0", "–", "–", "–", "–", "–", "–", "–", "–"])
            continue
        rows.append([b["title"], str(o["n"]), f"{o['win_rate']}%", _f(o["expectancy_r"], "{:+.3f}R"),
                     _f(o["profit_factor"], "{:.2f}"), _f(o["total_r"], "{:+.1f}R"), _f(o["max_dd_r"], "{:.1f}R"),
                     _f(o["sharpe_daily"], "{:.2f}"), _f(o["sqn"], "{:.2f}"),
                     ", ".join(f"{v} {k}" for k, v in Counter(s["verdict"] for s in b["setups"]).most_common())])
    lines += _table(["Board", "Trades", "Win", "Expectancy", "PF", "Total", "Max DD", "Sharpe (daily)", "SQN", "Setups"], rows)
    lines += ["", "**Findings**", ""]
    lines += [f"- {f}" for f in findings(report)] or ["- nothing stands out"]
    for kind, b in report["boards"].items():
        lines += ["", f"## {b['title']}", "", "### Setup performance", ""]
        rows = []
        for s in b["setups"]:
            if not s.get("n"):
                continue
            exits = ", ".join(f"{k} {v}" for k, v in sorted(s["exits"].items(), key=lambda kv: -kv[1]))
            eff = s.get("efficacy") or {}
            rows.append([f"{s['setup']} {s['side']}", str(s["n"]), f"{s['win_rate']}%", _f(s["expectancy_r"], "{:+.3f}"),
                         f"{_f(s['ci95'][0], '{:+.2f}')} … {_f(s['ci95'][1], '{:+.2f}')}", _f(s["median_r"], "{:+.2f}"),
                         _f(s["profit_factor"], "{:.2f}"), _f(s["payoff"], "{:.2f}"), _f(s["sqn"], "{:.2f}"),
                         _f(s["total_r"], "{:+.1f}"), _f(s["max_dd_r"], "{:.1f}"), str(s["losing_streak"]),
                         f"{_f(s['avg_mfe_r'], '{:.2f}')} / {_f(s['avg_mae_r'], '{:.2f}')}", _f(s["capture"], "{:.0f}%"),
                         _f(s["avg_cost_r"], "{:.3f}"), _f(s["avg_hold_bars"], "{:.1f}"), exits,
                         _f(s["recent_r"], "{:+.2f}"), _f(eff.get("adj"), "{:+d}"), s["verdict"]])
        lines += _table(["Setup", "N", "Win", "Exp R", "95% CI", "Median", "PF", "Payoff", "SQN", "Total R", "Max DD",
                         "Loss run", "MFE / MAE", "Capture", "Cost R", "Hold (bars)", "Exits", "Last 20", "Quality adj",
                         "Verdict"], rows) if rows else ["No closed trades in the window."]
        lines += ["", f"### Day by day (last {DAYS_IN_REPORT} sessions)", ""]
        rows = [[d["date"], str(d["trades"]), f"{round(d['wins'] / d['trades'] * 100)}%", _f(d["net_r"], "{:+.2f}"),
                 _f(d["cum_r"], "{:+.1f}"), f"{d['best'][0]} {d['best'][1]:+.2f}", f"{d['worst'][0]} {d['worst'][1]:+.2f}"]
                for d in b["daily"][-DAYS_IN_REPORT:]]
        lines += _table(["Date", "Trades", "Win", "Net R", "Cum R (window)", "Best setup", "Worst setup"], rows) \
            if rows else ["No sessions in the window."]
        live = b["live"][-DAYS_IN_REPORT:]
        if live:
            lines += ["", "### Live board (signals shown to users)", ""]
            lines += _table(["Date", "Signals", "Taken", "Skipped", "Closed", "Win", "Net R", "Auto-trade orders"],
                            [[d["date"], str(d["signals"]), str(d["taken"]), str(d["skipped"]), str(d["closed"]),
                              f"{round(d['wins'] / d['closed'] * 100)}%" if d["closed"] else "–",
                              _f(d["net_r"], "{:+.2f}"), str(d["orders"])] for d in live])
        lines += ["", "### Parameters (as the engine runs them today)", ""]
        p0 = b["parameters"][0] if b["parameters"] else {}
        shared = {k: v for k, v in p0.items() if k not in ("setup", "label", "enabled", "volume_min", "long", "short", "stop")}
        lines += [f"- **{k.replace('_', ' ')}**: {v}" for k, v in shared.items()]
        lines += [""] + _table(["Setup", "On", "Volume", "Long", "Short", "Stop"],
                               [[f"{p['setup']} ({p['label']})", "yes" if p["enabled"] else "no", p["volume_min"],
                                 p["long"], p["short"], p["stop"]] for p in b["parameters"]])
    lines += ["", "## Definitions", "",
              "- **Exp R**: mean net R per trade; **95% CI** of that mean (±1.96 standard errors). A CI wholly above 0 is a *proven edge*.",
              "- **PF**: gross won / gross lost. **Payoff**: average win / average loss. **SQN**: √min(N,100) × mean / std.",
              "- **Max DD**: worst peak-to-trough of cumulative R, in trade order. **Loss run**: longest run of losing trades.",
              "- **MFE / MAE**: average best / worst open R; **Capture**: expectancy as a share of MFE (exit efficiency).",
              "- **Cost R**: slippage and charges per trade in R. **Last 20**: mean R of the 20 most recent trades.",
              "- **Sharpe / Sortino (daily)**: daily net R, annualised over 252 sessions.",
              "- **Quality adj**: the points the setup's track record adds to or takes from its new signals (−15…+8); "
              "*suspended* setups are shown but not taken.",
              "", "Analytical tools, not investment advice."]
    return "\n".join(lines) + "\n"


def findings(report: dict[str, Any]) -> list[str]:
    out = []
    for kind, b in report["boards"].items():
        name = "Day Trade" if kind == "day" else "Swing"
        start = len(out)
        ranked = [s for s in b["setups"] if s.get("n", 0) >= MIN_SAMPLE]
        if ranked:
            best = max(ranked, key=lambda s: s["expectancy_r"])
            worst = min(ranked, key=lambda s: s["expectancy_r"])
            out.append(f"{name}: best {best['setup']} {best['side']} {best['expectancy_r']:+.3f}R over {best['n']} "
                       f"({best['verdict']}); weakest {worst['setup']} {worst['side']} {worst['expectancy_r']:+.3f}R ({worst['verdict']})")
        for s in b["setups"]:
            if s["verdict"] == "suspended":
                out.append(f"{name}: {s['setup']} {s['side']} is suspended by its track record")
            elif s["verdict"] == "positive, fading":
                out.append(f"{name}: {s['setup']} {s['side']} is fading - last 20 {s['recent_r']:+.2f}R vs {s['expectancy_r']:+.2f}R")
            elif s.get("capture") is not None and s.get("n", 0) >= MIN_SAMPLE and s["capture"] < 15 and (s.get("avg_mfe_r") or 0) > 0.8:
                out.append(f"{name}: {s['setup']} {s['side']} gives back most of its move (MFE {s['avg_mfe_r']:.2f}R, "
                           f"keeps {s['capture']:.0f}%) - exits worth reviewing")
        o = b["overall"]
        if o.get("n", 0) >= MIN_SAMPLE:
            positive = [s for s in ranked if s["expectancy_r"] > 0]
            lo, hi = o["ci95"]
            edge = ("a proven edge" if lo is not None and lo > 0 else "a proven loss" if hi is not None and hi < 0
                    else "no proven edge")
            out.insert(start, f"{name}: {o['expectancy_r']:+.3f}R a trade over {o['n']} trades ({edge}); "
                          f"{len(positive)} of {len(ranked)} well-sampled setups positive")
            exits = Counter()
            for s in b["setups"]:
                exits.update(s.get("exits") or {})
            total = sum(exits.values())
            timed = exits.get("TIME EXIT", 0)
            if total and timed / total > 0.6:
                t2 = exits.get("TARGET 2", 0)
                out.append(f"{name}: {timed / total:.0%} of trades end on the time exit and {t2 / total:.1%} reach target 2 - "
                           f"targets are rarely reached within the holding time; review target distance or hold length")
        if o.get("n") and o.get("cost_drag") and o["cost_drag"] > 25:
            out.append(f"{name}: costs take {o['cost_drag']:.0f}% of the gross move - fewer, larger trades would keep more")
    return out


def report_for(days: int = 60, today: Optional[date] = None, db_path: Optional[str] = None) -> dict[str, Any]:
    """The report from the saved replay trades and the live ledger (what the admin panel shows)."""
    today = today or date.today()
    as_of, trades = load_trades()
    db_path = db_path or os.path.join(ROOT, "data", "market_data.db")
    ledger = live_ledger(db_path, (today - timedelta(days=days)).isoformat())
    return build(trades, today, days, ledger, as_of=as_of)
