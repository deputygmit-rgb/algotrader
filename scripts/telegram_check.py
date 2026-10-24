"""Check the private Telegram channel end to end, and the access-request opt-in.

Run on the server from the app directory:
    cd /opt/verquoai/app
    sudo -u verquo /opt/verquoai/venv/bin/python scripts/telegram_check.py            settings, bot, channel
    ... telegram_check.py --chat-id          list the chats the bot was added to (find the -100... id)
    ... telegram_check.py --send             also post a test message and a sample signal
    ... telegram_check.py --invite 42        email request #42 its invitation now (skips the 7-day wait)
    ... telegram_check.py --invite-all       invite every request not yet invited, now (any age,
                                             approved included, rejected skipped); --dry-run counts only
    ... telegram_check.py --welcome          post and pin the welcome message (again: updates it in place)

Never prints the bot token. Exit status 0 when everything checked is fine.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from src.notify import telegram  # noqa: E402

SAMPLE_SIGNAL = {"id": "check", "symbol": "SAMPLE", "side": "LONG", "label": "Opening-range breakout",
                 "trigger": "TEST MESSAGE - not a real signal", "quality": 0, "entry": 100.0, "stop": 98.0,
                 "target1": 103.0, "target2": 106.0, "time": "--:--"}
SAMPLE_FLOW = {"rows": [{"symbol": "SAMPLE_L", "change_pct": 1.0, "oi_change_pct": 2.0, "buildup": "LONG BUILD-UP",
                         "flow_score": 50},
                        {"symbol": "SAMPLE_S", "change_pct": -1.0, "oi_change_pct": 2.0, "buildup": "SHORT BUILD-UP",
                         "flow_score": -50}],
               "picks": {"fno": {"long": ["SAMPLE_L"], "short": ["SAMPLE_S"]}}}


def ok(label: str, good: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if good else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    return good


def call(token: str, method: str, **params: object) -> dict:
    r = httpx.post(f"{telegram.API}/bot{token}/{method}", json=params, timeout=10.0)
    data = r.json() if r.content else {}
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description') or r.status_code}")
    return data["result"]


def list_chats(token: str) -> None:
    """Chats the bot has seen recently (post something in the channel first)."""
    updates = call(token, "getUpdates", allowed_updates=["channel_post", "my_chat_member", "message"])
    seen = {}
    for u in updates:
        for key in ("channel_post", "my_chat_member", "message"):
            chat = (u.get(key) or {}).get("chat")
            if chat:
                seen[chat["id"]] = (chat.get("type"), chat.get("title") or chat.get("username") or "")
    if not seen:
        print("No chats yet: add the bot to the channel as an admin, post any message there, run this again.")
    for chat_id, (kind, title) in seen.items():
        print(f"  {chat_id}  {kind}  {title}")


def check_channel(send: bool) -> bool:
    print("Settings")
    problem = telegram.config_problem()
    good = ok("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", not problem, problem)
    from src.api.webapp import telegram_channel_url

    good &= ok("TELEGRAM_CHANNEL_URL is a https://t.me/ invite link", bool(telegram_channel_url()))
    good &= ok("SESSION_SECRET is set (confirm links survive restarts)", bool(os.environ.get("SESSION_SECRET")))
    if problem:
        return False
    token, chat_id = telegram._config()

    print("Bot and channel")
    try:
        me = call(token, "getMe")
        ok("bot token accepted", True, f"@{me.get('username')}")
        chat = call(token, "getChat", chat_id=chat_id)
        good &= ok("chat is a channel", chat.get("type") == "channel", chat.get("type", ""))
        good &= ok("channel is private (no public @username)", not chat.get("username"),
                   f"public as @{chat['username']}" if chat.get("username") else "")
        member = call(token, "getChatMember", chat_id=chat_id, user_id=me["id"])
        good &= ok("bot is an administrator", member.get("status") == "administrator", member.get("status", ""))
        good &= ok("bot can post messages", bool(member.get("can_post_messages")))
        extra = [k for k in ("can_delete_messages", "can_edit_messages", "can_invite_users", "can_restrict_members",
                             "can_promote_members", "can_change_info", "can_manage_chat") if member.get(k)]
        if extra:      # advice, not a failure: posting works either way
            print("  [NOTE] bot also has " + ", ".join(extra) + " - only \"Post messages\" is needed")
        else:
            ok("bot has only the rights it needs", True)
        count = call(token, "getChatMemberCount", chat_id=chat_id)
        ok("members", True, str(count))
    except (RuntimeError, httpx.HTTPError) as e:
        return ok("Telegram API", False, str(e))

    if send:
        print("Posting")
        good &= ok("test message posted",
                   asyncio.run(telegram.send("VerquoAI channel check - test message, please ignore.")))
        good &= ok("sample signal posted (formatted like a real one)",
                   asyncio.run(telegram.send(telegram.format_signal("day", SAMPLE_SIGNAL, SAMPLE_FLOW))))
    return good


def invite(contact_id: int) -> bool:
    from src.api import webapp
    from src.metrics.store import metrics

    print(f"Invitation for request #{contact_id}")
    if not ok("channel ready (posting, invite link, SESSION_SECRET)", webapp.channel_ready()):
        return False
    row = metrics.get_contact(contact_id)
    if not ok("request exists", bool(row)):
        return False
    if not ok("request is not rejected", row["status"] != "rejected", row["status"]):
        return False
    sent = webapp.send_mail(row["email"],
                            f"Your VerquoAI access request #{contact_id} - join our private Telegram channel?",
                            webapp._access_followup(contact_id))
    if sent:
        metrics.mark_followup(contact_id)
    return ok("invitation emailed (open it and press the button to finish the test)", sent)


def invite_all(gap_s: float, dry_run: bool) -> bool:
    """One invitation to every request not yet invited, paced so the mail provider sees no burst.
    Safe to stop and run again: each request is marked as it is sent."""
    import time

    from src.api import webapp
    from src.metrics.store import metrics

    pending = metrics.due_followups(days=0, limit=100_000, skip_statuses=("rejected",))
    print(f"{len(pending)} request(s) not yet invited (rejected ones skipped).")
    if dry_run or not pending:
        return True
    if not ok("channel ready (posting, invite link, SESSION_SECRET)", webapp.channel_ready()):
        return False
    print(f"Sending one every {gap_s:g}s - about {len(pending) * gap_s / 60:.0f} minutes. Ctrl+C stops; run again to resume.")
    sent = failed = 0
    for i, row in enumerate(pending, 1):
        subject = f"Your VerquoAI access request #{row['id']} - join our private Telegram channel?"
        if webapp.send_mail(row["email"], subject, webapp._access_followup(row["id"])):
            metrics.mark_followup(row["id"])
            sent += 1
        else:
            failed += 1
            if failed >= 5 and not sent:
                return ok("mail is being sent", False, "5 failures in a row - check SMTP, then run again")
        print(f"  {i}/{len(pending)}  sent {sent}  failed {failed}", end="\r", flush=True)
        if i < len(pending):
            time.sleep(gap_s)
    print()
    return ok(f"{sent} invitation(s) sent", failed == 0, f"{failed} failed - run again to retry them" if failed else "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chat-id", action="store_true", help="list chats the bot has seen, to find the channel id")
    parser.add_argument("--send", action="store_true", help="post a test message and a sample signal")
    parser.add_argument("--invite", type=int, metavar="ID", help="email this access request its invitation now")
    parser.add_argument("--welcome", action="store_true", help="post and pin the welcome message, or update it")
    parser.add_argument("--invite-all", action="store_true", help="invite every request not yet invited, now")
    parser.add_argument("--gap", type=float, default=10.0, help="seconds between invitation emails (default 10)")
    parser.add_argument("--dry-run", action="store_true", help="with --invite-all: only count")
    args = parser.parse_args()
    if args.invite_all:
        sys.exit(0 if invite_all(max(args.gap, 2.0), args.dry_run) else 1)
    if args.welcome:
        if telegram.config_problem():
            sys.exit(telegram.config_problem())
        outcome = asyncio.run(telegram.post_welcome())
        print(f"Welcome message {outcome} and pinned." if outcome else "Could not post the welcome message (see log).")
        sys.exit(0 if outcome else 1)
    if args.chat_id:
        token = telegram._clean("TELEGRAM_BOT_TOKEN")
        if not telegram.TOKEN_RE.fullmatch(token):
            sys.exit("Set a valid TELEGRAM_BOT_TOKEN in .env first.")
        list_chats(token)
        return
    good = invite(args.invite) if args.invite else check_channel(args.send)
    print("\nAll good." if good else "\nFix the FAIL lines above.")
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
