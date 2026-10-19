"""Add, update, remove or list VerquoAI users (data/users.json).

Run on the server from the app directory as the app user, then restart:
    cd /opt/verquoai/app
    sudo -u verquo /opt/verquoai/venv/bin/python scripts/manage_users.py list
    sudo -u verquo /opt/verquoai/venv/bin/python scripts/manage_users.py add AB1234 trader@example.com
    sudo -u verquo /opt/verquoai/venv/bin/python scripts/manage_users.py update AB1234 [new-email]
    sudo -u verquo /opt/verquoai/venv/bin/python scripts/manage_users.py remove AB1234
    sudo systemctl restart verquoai

The Kite API key and secret are asked for at a hidden prompt (never on the
command line, so they stay out of shell history) and saved encrypted.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # the app resolves data/ relative to its directory

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

ZERODHA_ID = re.compile(r"^[A-Z0-9]{4,10}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _check_key() -> None:
    # Without the key, user_config would generate a new one and the existing
    # users could no longer be decrypted.
    if not os.environ.get("USER_CONFIG_ENCRYPTION_KEY") and not os.path.exists("data/.user_config.key"):
        if os.path.exists("data/users.json"):
            sys.exit("USER_CONFIG_ENCRYPTION_KEY is not set and no key file exists - refusing to continue.")


def _zerodha_id(value: str) -> str:
    uid = value.strip().upper()
    if not ZERODHA_ID.match(uid):
        sys.exit(f"Invalid Zerodha ID: {value!r}")
    return uid


def _email(value: str) -> str:
    email = value.strip().lower()
    if not EMAIL.match(email):
        sys.exit(f"Invalid email: {value!r}")
    return email


def _ask_credentials() -> tuple[str, str]:
    api_key = getpass.getpass("Kite API key: ").strip()
    api_secret = getpass.getpass("Kite API secret: ").strip()
    if not api_key or not api_secret:
        sys.exit("API key and secret are both required.")
    return api_key, api_secret


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("zerodha_id")
    add.add_argument("email")
    update = sub.add_parser("update", help="replace the API key/secret, optionally the email")
    update.add_argument("zerodha_id")
    update.add_argument("email", nargs="?")
    remove = sub.add_parser("remove")
    remove.add_argument("zerodha_id")
    args = parser.parse_args()

    _check_key()
    from src.auth.user_config import UserConfigService

    users = UserConfigService.get_instance()
    if os.path.exists("data/users.json"):
        try:
            with open("data/users.json") as f:
                saved = json.load(f)
        except ValueError:
            sys.exit("data/users.json is not valid JSON - not changing it.")
        if len(users.list_users()) != len(saved):
            sys.exit("Could not decrypt data/users.json (wrong USER_CONFIG_ENCRYPTION_KEY?) - not changing it.")

    if args.command == "list":
        for u in users.list_users():
            print(f"{u['zerodha_id']:<10} {u['email']}")
        return

    uid = _zerodha_id(args.zerodha_id)
    existing = users.get_user(uid)

    if args.command == "add":
        if existing:
            sys.exit(f"{uid} already exists - use 'update' to change its credentials.")
        email = _email(args.email)
        api_key, api_secret = _ask_credentials()
        users.add_user(uid, api_key, api_secret, email)
    elif args.command == "update":
        if not existing:
            sys.exit(f"{uid} does not exist - use 'add'.")
        email = _email(args.email) if args.email else existing.email
        api_key, api_secret = _ask_credentials()
        users.add_user(uid, api_key, api_secret, email)
    elif args.command == "remove":
        if not existing or not users.remove_user(uid):
            sys.exit(f"{uid} does not exist.")

    print(f"{args.command}: {uid} done. Restart the app: sudo systemctl restart verquoai")


if __name__ == "__main__":
    main()
