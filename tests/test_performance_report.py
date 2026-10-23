"""Strategy performance report (src/analysis/performance_report.py): the statistics,
verdicts, per-setup / per-day grouping, the live board, the Markdown, the nightly
attachment, the Swing ledger, and the admin panel's access to it."""
import asyncio
import os
from datetime import date
from types import SimpleNamespace

import pytest

from src.analysis import performance_report as perf

TODAY = date(2026, 10, 23)


def _t(r, d="2026-10-20", setup="ORB", side="LONG", kind="day", **kw):
    return {"kind": kind, "setup": setup, "side": side, "date": d, "symbol": "INFY", "result_r": r,
            "gross_r": r + 0.1, "cost_r": 0.1, "mfe_r": max(r, 0) + 0.5, "mae_r": min(r, 0) - 0.2, "bars_held": 10,
            "status": "TARGET 2" if r > 1 else "STOPPED" if r < 0 else "TIME EXIT", "time": "10:05", **kw}


def test_statistics_of_a_known_sequence():
    s = perf.stats([_t(2.0, "2026-10-01"), _t(-1.0, "2026-10-02"), _t(-1.0, "2026-10-03"), _t(1.5, "2026-10-04")])
    assert s["n"] == 4 and s["win_rate"] == 50.0 and s["expectancy_r"] == 0.375 and s["total_r"] == 1.5
    assert s["profit_factor"] == 1.75 and s["payoff"] == 1.75 and s["losing_streak"] == 2
    assert s["max_dd_r"] == -2.0 and s["best_r"] == 2.0 and s["worst_r"] == -1.0
    assert s["ci95"][0] < s["expectancy_r"] < s["ci95"][1] and s["exits"] == {"TARGET 2": 2, "STOPPED": 2}
    assert s["avg_cost_r"] == 0.1 and s["days"] == 4 and s["capture"] is not None
    assert perf.stats([]) == {"n": 0}


def test_verdicts_follow_the_evidence():
    win = [_t(0.5, f"2026-09-{d:02d}") for d in range(1, 29)] + [_t(0.4, "2026-10-01"), _t(0.6, "2026-10-02")]
    assert perf.verdict(perf.stats(win)) == "proven edge"                       # CI wholly above 0
    assert perf.verdict(perf.stats([_t(-0.5)] * 30 + [_t(-0.6)])) == "losing"
    assert perf.verdict(perf.stats([_t(1.0)] * 10)) == "too few trades"
    assert perf.verdict(perf.stats(win), {"status": "suspended"}) == "suspended"
    mixed = [_t(r, f"2026-09-{i % 28 + 1:02d}") for i, r in enumerate([2.0, -1.0] * 20)] + [_t(-1.0, "2026-10-10")] * 20
    assert perf.verdict(perf.stats(mixed)) in ("positive, fading", "no edge")


def test_the_report_groups_by_board_setup_and_day_and_keeps_to_the_window():
    trades = [_t(1.0, "2026-10-20"), _t(-1.0, "2026-10-20", setup="SQUEEZE", side="SHORT"), _t(0.5, "2026-10-21"),
              _t(3.0, "2026-10-23"),                          # today: not closed before the report
              _t(9.0, "2026-01-01"),                          # outside the window
              _t(0.8, "2026-10-21", kind="swing", setup="FLAG")]
    ledger = [{"source": "board", "session": "2026-10-21", "account": "AB1234", "result_r": 1.0, "skipped": "",
               "backfilled": 0, "decision": "sent"},
              {"source": "board", "session": "2026-10-21", "account": "AB1234", "result_r": None, "skipped": "portfolio full",
               "backfilled": 0, "decision": ""},
              {"source": "board", "session": "2026-10-21", "account": "AB1234", "result_r": -5.0, "skipped": "",
               "backfilled": 1, "decision": ""},
              {"source": "swing", "session": "2026-10-21", "account": "AB1234", "result_r": 0.5, "skipped": "",
               "backfilled": 0, "decision": ""}]
    r = perf.build(trades, TODAY, days=30, ledger=ledger, efficacy={})
    day = r["boards"]["day"]
    assert day["overall"]["n"] == 3 and {(s["setup"], s["side"]) for s in day["setups"]} == {("ORB", "LONG"), ("SQUEEZE", "SHORT")}
    assert [(d["date"], d["net_r"], d["cum_r"]) for d in day["daily"]] == [("2026-10-20", 0.0, 0.0), ("2026-10-21", 0.5, 0.5)]
    assert day["daily"][0]["best"] == ["ORB LONG", 1.0] and day["daily"][0]["worst"] == ["SQUEEZE SHORT", -1.0]
    (live,) = day["live"]
    assert (live["signals"], live["taken"], live["skipped"], live["closed"], live["net_r"], live["orders"]) == (2, 1, 1, 1, 1.0, 1)
    assert r["boards"]["swing"]["overall"]["n"] == 1 and r["boards"]["swing"]["live"][0]["net_r"] == 0.5
    assert {p["setup"] for p in day["parameters"]} >= {"ORB", "PD_BREAK", "VWAP_RECLAIM", "SQUEEZE"}
    assert {p["setup"] for p in r["boards"]["swing"]["parameters"]} == {"BREAKOUT_20D", "TREND_PULLBACK", "INSIDE_DAY", "FLAG"}


def test_markdown_has_every_section_and_escapes_table_cells():
    trades = [_t(r, f"2026-10-{d:02d}") for d, r in zip(range(1, 21), [1.0, -1.0] * 10)]
    trades += [_t(0.2, "2026-10-05", setup="ORB|X")]
    md = perf.markdown(perf.build(trades, TODAY, days=60, ledger=[], efficacy={}))
    for heading in ("# VerquoAI strategy performance — 2026-10-23", "## Summary", "## Day Trade", "### Setup performance",
                    "### Day by day", "### Parameters", "## Swing", "## Definitions"):
        assert heading in md, heading
    assert "ORB\\|X LONG" in md and "| ORB LONG | 20 | 50.0% |" in md


def test_findings_point_at_time_exits_and_the_overall_edge():
    trades = [_t(0.1 if i % 2 else -0.1, f"2026-09-{i % 28 + 1:02d}", status="TIME EXIT") for i in range(40)]
    found = perf.findings(perf.build(trades, TODAY, days=60, ledger=[], efficacy={}))
    assert found[0].startswith("Day Trade: +0.000R a trade over 40 trades")
    assert any("end on the time exit" in f for f in found)


def test_the_nightly_job_keeps_full_trade_details(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("rebuild", os.path.join(os.path.dirname(__file__), "..", "scripts", "rebuild_efficacy.py"))
    rebuild = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rebuild)
    t = rebuild._trade("day", "INFY", {"setup": "ORB", "side": "LONG", "quality": 72.0, "status": "TARGET 2",
                                       "gross_r": 3.1234, "cost_r": 0.0912, "result_r": 3.0322, "mfe_r": 3.5,
                                       "mae_r": -0.2, "bars_held": 14, "time": "10:05", "exit_time": "11:15"}, "2026-10-20")
    assert t == {"kind": "day", "symbol": "INFY", "setup": "ORB", "side": "LONG", "date": "2026-10-20", "quality": 72.0,
                 "status": "TARGET 2", "gross_r": 3.123, "cost_r": 0.091, "result_r": 3.032, "mfe_r": 3.5,
                 "mae_r": -0.2, "bars_held": 14, "time": "10:05", "exit_time": "11:15"}
    perf.save_trades([t], TODAY, path=str(tmp_path / "t.json"))
    assert perf.load_trades(str(tmp_path / "t.json")) == ("2026-10-23", [t])


def test_reports_are_mailed_with_the_markdown_attached(monkeypatch):
    from src.utils import mailer

    sent = []

    class SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)
    monkeypatch.setenv("SMTP_EMAIL", "bot@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "x")
    monkeypatch.setattr(mailer.smtplib, "SMTP", SMTP)
    assert mailer.send_mail("a@example.com", "Report", "body", attachments=[("r.md", "# Report\n", "markdown")])
    (att,) = list(sent[0].iter_attachments())
    assert att.get_filename() == "r.md" and att.get_content_type() == "text/markdown" and att.get_content() == "# Report\n"


def test_swing_board_signals_reach_the_ledger_once_per_status():
    from src.api.service import TradingService

    rows = []
    svc = TradingService.__new__(TradingService)
    svc._zerodha_id = "AB1234"
    svc._market_store = SimpleNamespace(upsert_signal=lambda key, row, now: rows.append((key, row["status"], row["source"])))
    sig = {"id": "INFY:1", "symbol": "INFY", "bar_time": "2026-10-21T10:15:00", "time": "2026-10-21 11:15",
           "side": "LONG", "setup": "FLAG", "status": "PENDING"}
    svc._ledger_swing({"feed": [sig]})
    svc._ledger_swing({"feed": [sig]})                       # unchanged: not written again
    svc._ledger_swing({"feed": [{**sig, "status": "TARGET 2", "result_r": 2.9}]})
    assert rows == [("swing|AB1234|INFY:1", "PENDING", "swing"), ("swing|AB1234|INFY:1", "TARGET 2", "swing")]


# ── admin panel ───────────────────────────────────────────────────────────
from tests.test_admin_panel import call, env, session  # noqa: E402,F401  (fixture and helpers)


@pytest.fixture
def saved(env, monkeypatch, tmp_path):
    monkeypatch.setattr(perf, "TRADES_PATH", str(tmp_path / "setup_trades.json"))
    monkeypatch.setattr(perf, "REPORT_DIR", str(tmp_path / "reports"))
    os.makedirs(tmp_path / "reports")
    (tmp_path / "reports" / "performance-2026-10-22.md").write_text("# nightly\n")
    perf.save_trades([_t(1.0, "2026-10-20"), _t(-0.5, "2026-10-21")], TODAY, path=str(tmp_path / "setup_trades.json"))
    monkeypatch.setattr(perf, "report_for", lambda days, today=None, db_path=None: perf.build(
        perf.load_trades(str(tmp_path / "setup_trades.json"))[1], TODAY, days, [], efficacy={}, as_of="2026-10-23"))
    return env


def test_only_a_confirmed_admin_sees_the_performance_report(saved):
    assert call("GET", "/api/admin/performance").status_code == 401
    assert call("GET", "/api/admin/performance", session("US5678", step_up=True)).status_code == 404
    assert call("GET", "/api/admin/performance", session("AD1234")).status_code == 403          # no confirmation code
    res = call("GET", "/api/admin/performance?days=30", session("AD1234", step_up=True))
    body = res.json()
    assert res.status_code == 200 and body["boards"]["day"]["overall"]["n"] == 2 and body["windows"] == [30, 60, 90, 120]
    assert call("GET", "/api/admin/performance?days=7", session("AD1234", step_up=True)).status_code == 400


def test_the_admin_downloads_and_emails_the_markdown(saved, monkeypatch):
    token = session("AD1234", step_up=True)
    md = call("GET", "/api/admin/performance.md?days=60", token)
    assert md.headers["content-type"].startswith("text/markdown") and "attachment" in md.headers["content-disposition"]
    assert md.text.startswith("# VerquoAI strategy performance")
    mails = []
    monkeypatch.setattr("src.utils.mailer.send_mail", lambda to, subj, text, reply, html, att: mails.append((to, att[0][0])) or True)
    res = call("POST", "/api/admin/performance/email", token, {"days": 60})
    assert res.json() == {"success": True, "to": "admin@example.com"} and mails[0][1].endswith("-60d.md")
    actions = [a["action"] for a in saved.store.list_audit(10)]
    assert "performance_report_emailed" in actions and "performance_report_downloaded" in actions


def test_nightly_reports_are_listed_and_only_their_own_files_served(saved):
    token = session("AD1234", step_up=True)
    assert call("GET", "/api/admin/performance/archive", token).json() == {
        "reports": [{"name": "performance-2026-10-22.md", "date": "2026-10-22"}]}
    assert call("GET", "/api/admin/performance/archive/performance-2026-10-22.md", token).text == "# nightly\n"
    for bad in ("..%2Fusers.json", "users.json", "performance-2026-10-22.md.bak"):
        assert call("GET", f"/api/admin/performance/archive/{bad}", token).status_code == 404
