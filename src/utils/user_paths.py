"""Per-user data file locations.

Journals and saved strategies belong to one Zerodha account, so each user gets
``data/users/<ZERODHA_ID>/``. Files from before this split lived directly in
``data/`` / ``logs/``; they are copied to the owner's folder only when exactly
one user is configured (otherwise ownership is ambiguous and the user starts
empty). The legacy file is left in place.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3

from src.utils.logger import get_logger

logger = get_logger(__name__)

USERS_DATA_DIR = os.path.join("data", "users")


def _safe_id(zerodha_id: str) -> str:
    return re.sub(r"[^A-Z0-9_-]", "", (zerodha_id or "").upper()) or "DEFAULT"


def _is_sole_user(uid: str) -> bool:
    from src.auth.user_config import UserConfigService

    users = UserConfigService.get_instance().list_users()
    return len(users) == 1 and _safe_id(users[0]["zerodha_id"]) == uid


def _copy(src: str, dst: str) -> None:
    if src.endswith(".db"):
        # The backup API includes pages still in the -wal file.
        with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
            source.backup(target)
    else:
        shutil.copy2(src, dst)


def user_data_path(zerodha_id: str, filename: str, legacy_path: str | None = None) -> str:
    uid = _safe_id(zerodha_id)
    path = os.path.join(USERS_DATA_DIR, uid, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if legacy_path and not os.path.exists(path) and os.path.exists(legacy_path):
        if _is_sole_user(uid):
            _copy(legacy_path, path)
            logger.info("user_data_migrated", zerodha_id=uid, source=legacy_path, target=path)
        else:
            logger.warning("user_data_not_migrated", zerodha_id=uid, source=legacy_path,
                           reason="legacy file has no single owner")
    return path
