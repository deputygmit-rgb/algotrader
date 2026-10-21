from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from src.utils.logger import get_logger
from src.auth.access import AGENTIC, LEGACY_DEFAULT, LEVELS, NEW_ACCOUNT_DEFAULT, normalize_level

logger = get_logger(__name__)

USER_CONFIG_FILE = "data/users.json"
_KEY_FILE = "data/.user_config.key"
_KEY_ENV_VAR = "USER_CONFIG_ENCRYPTION_KEY"


def _load_or_create_key() -> bytes:
    """Resolve the Fernet key used to encrypt api_key/api_secret at rest.

    Prefers the USER_CONFIG_ENCRYPTION_KEY env var (recommended for production
    - keep the key out of the data directory, e.g. in your secrets manager).
    Falls back to a locally generated, owner-only-readable key file for local/dev use.
    """
    env_key = os.environ.get(_KEY_ENV_VAR)
    if env_key:
        if os.path.exists(_KEY_FILE):
            logger.warning(
                "user_config_key_file_unused",
                key_file=_KEY_FILE,
                hint=f"{_KEY_ENV_VAR} is set; delete the old key file so the key is not stored next to the data.",
            )
        return env_key.encode()
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(_KEY_FILE), exist_ok=True)
    with open(_KEY_FILE, "wb") as f:
        f.write(key)
    os.chmod(_KEY_FILE, 0o600)
    logger.warning(
        "user_config_key_generated",
        key_file=_KEY_FILE,
        hint=f"For production, set {_KEY_ENV_VAR} instead of relying on this local key file.",
    )
    return key


_fernet = Fernet(_load_or_create_key())


# Every Fernet token starts with this (version byte 0x80, base64-encoded).
_FERNET_PREFIX = "gAAAAA"


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet.encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> tuple[str, bool]:
    """Returns (plaintext, was_legacy_plaintext).

    Raises ValueError when the value is a Fernet token this key cannot open
    (wrong or rotated key) instead of mistaking the ciphertext for plaintext.
    """
    if not value:
        return "", False
    try:
        return _fernet.decrypt(value.encode()).decode(), False
    except InvalidToken:
        if value.startswith(_FERNET_PREFIX):
            raise ValueError(
                f"Encrypted value cannot be decrypted - check {_KEY_ENV_VAR} matches the key it was saved with"
            ) from None
        # Pre-encryption value written before this feature existed - use as-is,
        # caller should re-save to migrate it to encrypted form.
        return value, True


_encrypt = encrypt_secret
_decrypt = decrypt_secret


class UserConfig:
    def __init__(self, zerodha_id: str, api_key: str, api_secret: str, email: str = "",
                 access_level: str = LEGACY_DEFAULT) -> None:
        self.zerodha_id = zerodha_id.upper()
        self.api_key = api_key
        self.api_secret = api_secret
        self.email = email
        # view | trade | agentic - see src/auth/access.py
        self.access_level = normalize_level(access_level)


class UserConfigService:
    _instance: Optional[UserConfigService] = None

    def __init__(self) -> None:
        self._users: dict[str, UserConfig] = {}
        self._mtime = 0.0
        self._checked = 0.0
        self._load()

    @classmethod
    def get_instance(cls) -> UserConfigService:
        if cls._instance is None:
            cls._instance = UserConfigService()
        return cls._instance

    @staticmethod
    def _read_file() -> tuple[dict[str, UserConfig], bool]:
        """Users on disk, and whether any secret is still stored in plain text.
        Raises when the file cannot be read or decrypted."""
        with open(USER_CONFIG_FILE, "r") as f:
            data = json.load(f)
        users: dict[str, UserConfig] = {}
        needs_migration = False
        for uid, info in data.items():
            api_key, legacy_key = _decrypt(info.get("api_key", ""))
            api_secret, legacy_secret = _decrypt(info.get("api_secret", ""))
            needs_migration = needs_migration or legacy_key or legacy_secret
            users[uid.upper()] = UserConfig(
                zerodha_id=uid.upper(),
                api_key=api_key,
                api_secret=api_secret,
                email=info.get("email", ""),
                # accounts saved before access levels existed keep full access
                access_level=normalize_level(info.get("access_level"), LEGACY_DEFAULT),
            )
        return users, needs_migration

    def reload(self) -> None:
        """Re-read users.json (picks up scripts/manage_users.py edits). Raises on
        failure and keeps the users already in memory."""
        if not os.path.exists(USER_CONFIG_FILE):
            return
        users, _ = self._read_file()
        self._users = users
        self._mtime = os.path.getmtime(USER_CONFIG_FILE)

    def _maybe_reload(self) -> None:
        now = time.monotonic()
        if now - self._checked < 5:
            return
        self._checked = now
        try:
            if os.path.exists(USER_CONFIG_FILE) and os.path.getmtime(USER_CONFIG_FILE) != self._mtime:
                self.reload()
                logger.info("users_reloaded", count=len(self._users))
        except Exception as e:
            logger.error("user_config_reload_failed", error=str(e))

    def _load(self) -> None:
        if os.path.exists(USER_CONFIG_FILE):
            try:
                self._users, needs_migration = self._read_file()
                self._mtime = os.path.getmtime(USER_CONFIG_FILE)
                logger.info("users_loaded", count=len(self._users))
                if needs_migration:
                    logger.info("user_config_migrating_to_encrypted")
                    self._save()
            except Exception as e:
                logger.error("user_config_load_failed", error=str(e))
        else:
            # Bootstrap the first user from the environment. Both the Zerodha ID
            # and the login email must be configured explicitly: whoever controls
            # that mailbox can log in.
            api_key = os.environ.get("KITE_API_KEY", "")
            api_secret = os.environ.get("KITE_API_SECRET", "")
            default_id = os.environ.get("KITE_USER_ID", "").strip().upper()
            default_email = os.environ.get("KITE_USER_EMAIL", "").strip().lower()
            if api_key and default_id and default_email:
                self._users[default_id] = UserConfig(
                    zerodha_id=default_id,
                    api_key=api_key,
                    api_secret=api_secret,
                    email=default_email,
                    access_level=AGENTIC,       # the bootstrap account is the owner's
                )
                self._save()
                logger.info("default_user_created", zerodha_id=default_id)
            elif api_key:
                logger.warning(
                    "default_user_not_created",
                    hint="Set KITE_USER_ID and KITE_USER_EMAIL to bootstrap the first user.",
                )

    def _save(self) -> None:
        """Write users.json atomically (a crash mid-write cannot corrupt it).
        Raises on failure so callers never report a change that was not saved."""
        os.makedirs(os.path.dirname(USER_CONFIG_FILE), exist_ok=True)
        data = {}
        for uid, uc in self._users.items():
            data[uid] = {
                "api_key": _encrypt(uc.api_key),
                "api_secret": _encrypt(uc.api_secret),
                "email": uc.email,
                "access_level": uc.access_level,
            }
        tmp = f"{USER_CONFIG_FILE}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, USER_CONFIG_FILE)
            self._mtime = os.path.getmtime(USER_CONFIG_FILE)
        except Exception as e:
            logger.error("user_config_save_failed", error=str(e))
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def get_user(self, zerodha_id: str) -> Optional[UserConfig]:
        self._maybe_reload()
        return self._users.get(zerodha_id.upper())

    def validate_user(self, zerodha_id: str) -> bool:
        return zerodha_id.upper() in self._users

    def add_user(self, zerodha_id: str, api_key: str, api_secret: str, email: str = "",
                 access_level: str = NEW_ACCOUNT_DEFAULT) -> UserConfig:
        uc = UserConfig(zerodha_id=zerodha_id, api_key=api_key, api_secret=api_secret, email=email,
                        access_level=normalize_level(access_level, NEW_ACCOUNT_DEFAULT))
        self._users[uc.zerodha_id] = uc
        self._save()
        logger.info("user_added", zerodha_id=uc.zerodha_id, access_level=uc.access_level)
        return uc

    def set_access_level(self, zerodha_id: str, access_level: str) -> UserConfig:
        uc = self._users.get(zerodha_id.upper())
        if uc is None:
            raise KeyError(zerodha_id)
        if access_level not in LEVELS:
            raise ValueError(f"access level must be one of {', '.join(LEVELS)}")
        uc.access_level = access_level
        self._save()
        logger.info("user_access_changed", zerodha_id=uc.zerodha_id, access_level=access_level)
        return uc

    def set_credentials(self, zerodha_id: str, api_key: str, api_secret: str) -> UserConfig:
        uc = self._users.get(zerodha_id.upper())
        if uc is None:
            raise KeyError(zerodha_id)
        uc.api_key, uc.api_secret = api_key, api_secret
        self._save()
        logger.info("user_credentials_changed", zerodha_id=uc.zerodha_id)
        return uc

    def set_email(self, zerodha_id: str, email: str) -> UserConfig:
        uc = self._users.get(zerodha_id.upper())
        if uc is None:
            raise KeyError(zerodha_id)
        uc.email = email
        self._save()
        logger.info("user_email_changed", zerodha_id=uc.zerodha_id)
        return uc

    def remove_user(self, zerodha_id: str) -> bool:
        uid = zerodha_id.upper()
        if uid in self._users:
            del self._users[uid]
            self._save()
            return True
        return False

    def list_users(self) -> list[dict[str, str]]:
        self._maybe_reload()
        return [{"zerodha_id": uc.zerodha_id, "email": uc.email, "access_level": uc.access_level}
                for uc in self._users.values()]
