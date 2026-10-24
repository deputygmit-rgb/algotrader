"""Signals to the private Telegram channel; the 7-day update to access requests."""
import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from src.api import webapp
from src.metrics.store import IST, MetricsStore
from src.notify import telegram

TOKEN = "123456789:" + "A" * 35

FLOW = {
    "rows": [{"symbol": s, "change_pct": c, "oi_change_pct": o, "buildup": b, "flow_score": f}
             for s, c, o, b, f in [("INFY", 1.2, 3.4, "LONG BUILD-UP", 62), ("TCS", -0.8, 2.1, "SHORT BUILD-UP", -55),
                                   ("SBIN", 0.9, 1.1, "LONG BUILD-UP", 48)]],
    "picks": {"fno": {"long": ["INFY", "SBIN"], "short": ["TCS"]}},
}
SIGNAL = {"id": "2026-10-23 10:20-ORB-L", "symbol": "INFY", "side": "LONG", "setup": "ORB", "label": "Opening-range breakout",
          "trigger": "close 1,452.3 above the opening range high 1,448.0 on 2.1x volume", "quality": 78, "vol_x": 2.1,
          "tod_x": 1.6, "market_bias": "MILDLY BULLISH", "market_adj": 8, "entry": 1452.7, "stop": 1441.2,
          "target1": 1470.0, "target2": 1487.2, "time": "10:25", "status": "PENDING", "is_latest_bar": True}


def test_the_message_says_why_and_lists_money_flow_leaders_with_oi():
    text = telegram.format_signal("day", SIGNAL, FLOW)
    assert "DAY TRADE · 5-min · LONG INFY" in text and "Why: close 1,452.3 above" in text
    assert "NIFTY options-OI MILDLY BULLISH (+8)" in text and "T2 1,487.20" in text
    assert "top longs" in text and "INFY +1.20% · OI +3.40% LONG BUILD-UP" in text and "SBIN" in text
    assert "top shorts" in text and "TCS -0.80% · OI +2.10% SHORT BUILD-UP" in text
    assert "not a recommendation" in text
    assert "SWING · 1-hour" in telegram.format_signal("swing", SIGNAL, FLOW)


def test_each_signal_is_posted_once_and_only_when_new(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "SENT_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    posted = []

    async def fake_send(text):
        posted.append(text)
        return True
    monkeypatch.setattr(telegram, "send", fake_send)
    monkeypatch.setattr(telegram, "MIN_GAP_S", 0)
    replayed = {**SIGNAL, "id": "x-old", "backfilled": True}
    skipped = {**SIGNAL, "id": "x-skip", "skipped": "5 trades open"}
    older_bar = {**SIGNAL, "id": "x-bar", "is_latest_bar": False}

    async def go():
        pub = telegram.TelegramPublisher()
        n1 = pub.publish_new("day", {"feed": [SIGNAL, replayed, skipped, older_bar]}, FLOW)
        await asyncio.sleep(0.05)
        n2 = telegram.TelegramPublisher().publish_new("day", {"feed": [SIGNAL]}, FLOW)   # after a restart
        return n1, n2
    assert asyncio.run(go()) == (1, 0) and len(posted) == 1


def test_no_valid_configuration_no_posts(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-a-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    assert not telegram.enabled()
    assert telegram.TelegramPublisher().publish_new("day", {"feed": [SIGNAL]}, FLOW) == 0


def test_the_week_old_request_gets_one_update(tmp_path, monkeypatch):
    store = MetricsStore(str(tmp_path / "a.db"))
    monkeypatch.setattr(webapp, "metrics", store)
    old = store.add_contact(name="A", email="a@b.co", phone="9876543210", trading_id="", message="", source="landing",
                            visitor_id="", user_id="", ip="")
    new = store.add_contact(name="B", email="b@b.co", phone="9876543211", trading_id="", message="", source="landing",
                            visitor_id="", user_id="", ip="")
    done = store.add_contact(name="C", email="c@b.co", phone="9876543212", trading_id="", message="", source="landing",
                             visitor_id="", user_id="", ip="")
    store.set_contact_status(done, "approved")
    week_ago = (datetime.now(IST) - timedelta(days=8)).isoformat(timespec="seconds")
    with sqlite3.connect(store._db_path) as c:
        c.execute("UPDATE contact_requests SET ts = ? WHERE id IN (?, ?)", (week_ago, old, done))
    sent = []
    monkeypatch.setattr(webapp, "send_mail", lambda to, subject, text, *a, **k: sent.append((to, subject, text)) or True)

    monkeypatch.delenv("TELEGRAM_CHANNEL_URL", raising=False)
    assert asyncio.run(webapp.send_due_followups()) == 0                 # no channel link yet: waits
    monkeypatch.setenv("TELEGRAM_CHANNEL_URL", "https://t.me/+AbCdEf123")
    assert asyncio.run(webapp.send_due_followups()) == 1
    to, subject, text = sent[0]
    assert to == "a@b.co" and subject == f"Your VerquoAI access request #{old} - an update"
    assert "completing broker compliance" in text and "https://t.me/+AbCdEf123" in text and f"#{old}" in text
    assert asyncio.run(webapp.send_due_followups()) == 0                 # once only
