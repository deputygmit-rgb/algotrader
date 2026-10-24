"""Signals to the private Telegram channel; the 7-day invitation and opt-in for access requests."""
import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from src.api import webapp
from src.metrics.store import IST, MetricsStore
from src.notify import telegram

TOKEN = "123456789:" + "A" * 35


@pytest.fixture(autouse=True)
def _fresh_optin_limiter(monkeypatch):
    """The confirm page is rate limited per IP; every test client shares one IP."""
    monkeypatch.setattr(webapp, "optin_limiter", webapp.RequestLimiter(rate=100, burst=100))

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


def _requests(tmp_path, monkeypatch):
    store = MetricsStore(str(tmp_path / "a.db"))
    monkeypatch.setattr(webapp, "metrics", store)
    ids = [store.add_contact(name=n, email=f"{n.lower()}@b.co", phone=f"987654321{i}", trading_id="", message="",
                             source="landing", visitor_id="", user_id="", ip="") for i, n in enumerate("ABC")]
    store.set_contact_status(ids[2], "approved")
    week_ago = (datetime.now(IST) - timedelta(days=8)).isoformat(timespec="seconds")
    with sqlite3.connect(store._db_path) as c:
        c.execute("UPDATE contact_requests SET ts = ? WHERE id IN (?, ?)", (week_ago, ids[0], ids[2]))
    sent = []
    monkeypatch.setattr(webapp, "send_mail", lambda to, subject, text, *a, **k: sent.append((to, subject, text)) or True)
    return store, ids, sent


def _channel(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHANNEL_URL", "https://t.me/+AbCdEf123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")


def test_the_week_old_request_gets_one_invitation_without_the_link(tmp_path, monkeypatch):
    store, (old, new, done), sent = _requests(tmp_path, monkeypatch)
    for name in ("TELEGRAM_CHANNEL_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEGRAM_CHANNEL_URL", "https://t.me/+AbCdEf123")
    assert asyncio.run(webapp.send_due_followups()) == 0                 # channel not posting yet: waits
    _channel(monkeypatch)
    assert asyncio.run(webapp.send_due_followups()) == 1
    to, subject, text = sent[0]
    assert to == "a@b.co" and subject == f"Your VerquoAI access request #{old} - join our private Telegram channel?"
    assert "completing broker compliance" in text and f"#{old}" in text
    assert "5-minute" in text and "1-hour" in text and "top 5 long and top 5 short" in text and "open-interest" in text
    assert "read-only" in text and "cannot be forwarded" in text
    assert "t.me" not in text                                            # the link waits for their yes
    assert f"/telegram/join?c={old}&t={webapp._optin_token(old)}" in text
    assert asyncio.run(webapp.send_due_followups()) == 0                 # once only


def test_confirming_emails_the_invite_link_once(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    store, (old, new, done), sent = _requests(tmp_path, monkeypatch)
    _channel(monkeypatch)
    asyncio.run(webapp.send_due_followups())
    sent.clear()
    client = TestClient(webapp.app)
    token = webapp._optin_token(old)

    page = client.get(f"/telegram/join?c={old}&t={token}")              # a mail scanner opening it changes nothing
    assert page.status_code == 200 and "YES, EMAIL ME THE INVITE LINK" in page.text and "5-minute" in page.text
    assert not sent and not store.get_contact(old)["optin_at"]

    done_page = client.post("/telegram/join", data={"c": str(old), "t": token})
    assert done_page.status_code == 200 and "check your email" in done_page.text
    assert len(sent) == 1 and sent[0][0] == "a@b.co" and "https://t.me/+AbCdEf123" in sent[0][2]
    row = store.get_contact(old)
    assert row["optin_at"] and row["link_sent_at"]
    assert "t.me/" not in done_page.text                                 # the link goes to their inbox only

    again = client.post("/telegram/join", data={"c": str(old), "t": token})
    assert "already confirmed" in again.text and len(sent) == 1
    assert "already confirmed" in client.get(f"/telegram/join?c={old}&t={token}").text


def test_forged_or_uninvited_links_do_nothing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    store, (old, new, done), sent = _requests(tmp_path, monkeypatch)
    _channel(monkeypatch)
    asyncio.run(webapp.send_due_followups())
    sent.clear()
    client = TestClient(webapp.app)
    assert client.post("/telegram/join", data={"c": str(old), "t": "0" * 32}).status_code == 404       # forged
    assert client.post("/telegram/join", data={"c": str(new), "t": webapp._optin_token(new)}).status_code == 404  # not invited
    assert client.get("/telegram/join?c=abc&t=x").status_code == 404
    store.set_contact_status(old, "rejected")
    assert client.post("/telegram/join", data={"c": str(old), "t": webapp._optin_token(old)}).status_code == 404
    assert not sent


def test_a_failed_link_email_can_be_retried(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    store, (old, *_), sent = _requests(tmp_path, monkeypatch)
    _channel(monkeypatch)
    asyncio.run(webapp.send_due_followups())
    monkeypatch.setattr(webapp, "send_mail", lambda *a, **k: False)
    client = TestClient(webapp.app)
    data = {"c": str(old), "t": webapp._optin_token(old)}
    assert client.post("/telegram/join", data=data).status_code == 503
    assert store.get_contact(old)["optin_at"] and not store.get_contact(old)["link_sent_at"]
    monkeypatch.setattr(webapp, "send_mail", lambda *a, **k: True)
    assert "check your email" in client.post("/telegram/join", data=data).text


def test_a_bad_setting_is_named_not_silently_ignored(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", f'"{TOKEN}"')                  # quotes are tolerated
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "https://t.me/+AbCdEf123")       # the invite link by mistake
    assert "numeric id" in telegram.config_problem() and not telegram.enabled()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "8987338790")                     # someone's own chat with the bot
    assert "person's chat" in telegram.config_problem() and not telegram.enabled()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    assert telegram.config_problem() == "" and telegram.enabled()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot" + TOKEN)
    assert "not a bot token" in telegram.config_problem()


def test_hostile_confirm_requests_fail_safely(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    store, (old, *_), sent = _requests(tmp_path, monkeypatch)
    _channel(monkeypatch)
    asyncio.run(webapp.send_due_followups())
    sent.clear()
    client = TestClient(webapp.app, raise_server_exceptions=False)
    for c, t in (("1", "é" * 32), ("0", "a" * 32), ("-1", "a" * 32), ("9" * 40, "a" * 32), ("1 ", "a" * 32),
                 (str(old), webapp._optin_token(old).upper()), (str(old), webapp._optin_token(old) + "0")):
        assert client.get("/telegram/join", params={"c": c, "t": t}).status_code == 404
        assert client.post("/telegram/join", data={"c": c, "t": t}).status_code == 404
    assert client.post("/telegram/join", content=b"c=1&t=" + b"a" * 2000,
                       headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413
    page = client.get(f"/telegram/join?c={old}&t={webapp._optin_token(old)}")
    assert page.headers["cache-control"] == "no-store" and page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-frame-options"] == "DENY" and "noindex" in page.headers["x-robots-tag"]
    assert not sent


def test_links_expire_and_are_rate_limited(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    store, (old, *_), sent = _requests(tmp_path, monkeypatch)
    _channel(monkeypatch)
    asyncio.run(webapp.send_due_followups())
    url = f"/telegram/join?c={old}&t={webapp._optin_token(old)}"
    client = TestClient(webapp.app)
    with sqlite3.connect(store._db_path) as c:
        c.execute("UPDATE contact_requests SET followup_at = ? WHERE id = ?",
                  ((datetime.now(IST) - timedelta(days=webapp.OPTIN_VALID_DAYS + 1)).isoformat(timespec="seconds"), old))
    assert client.get(url).status_code == 404                              # invited over 30 days ago
    monkeypatch.setattr(webapp, "optin_limiter", webapp.RequestLimiter(rate=0.0001, burst=3))
    codes = [client.get(url).status_code for _ in range(5)]
    assert codes[-1] == 429 and "Too many attempts" in client.get(url).text


def test_simultaneous_confirms_send_one_email(tmp_path, monkeypatch):
    store, (old, *_), sent = _requests(tmp_path, monkeypatch)
    assert store.claim_link_sent(old) is True and store.claim_link_sent(old) is False
    store.release_link_sent(old)
    assert store.claim_link_sent(old) is True


def test_the_welcome_is_posted_and_pinned_once_then_edited_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "SENT_DIR", str(tmp_path))
    monkeypatch.setattr(telegram, "WELCOME_FILE", str(tmp_path / "welcome.json"))
    calls = []

    async def fake_call(method, payload):
        calls.append(method)
        return {"message_id": 41} if method == "sendMessage" else True
    monkeypatch.setattr(telegram, "_call", fake_call)
    assert asyncio.run(telegram.post_welcome()) == "posted" and calls == ["sendMessage", "pinChatMessage"]
    calls.clear()
    assert asyncio.run(telegram.post_welcome()) == "updated" and calls == ["editMessageText"]   # no second post
    assert "never message you privately" in telegram.WELCOME and len(telegram.WELCOME) < 4096
    assert "5-minute" in telegram.WELCOME and "top 5 long" in telegram.WELCOME


def test_invite_everyone_now_skips_only_rejected_and_the_already_invited(tmp_path, monkeypatch):
    store, (old, new, done), sent = _requests(tmp_path, monkeypatch)            # done = approved
    rejected = store.add_contact(name="D", email="d@b.co", phone="9876543219", trading_id="", message="",
                                 source="landing", visitor_id="", user_id="", ip="")
    store.set_contact_status(rejected, "rejected")
    store.mark_followup(old)
    ids = [r["id"] for r in store.due_followups(days=0, limit=1000, skip_statuses=("rejected",))]
    assert ids == [new, done]                                                     # any age, approved included
    assert [r["id"] for r in store.due_followups(days=7)] == []                   # the hourly job is unchanged
