"""Settings an admin changes at runtime (data/app_settings.json).

Values here take precedence over the matching .env entry, so the admin panel
can change them without editing files on the server. Written atomically and
readable by the app user only.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SETTINGS_FILE = os.path.join(ROOT, "data", "app_settings.json")
_lock = threading.Lock()


def _read() -> dict[str, Any]:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_setting(name: str, env_name: str | None = None, default: str = "") -> str:
    """The admin-set value, else the environment variable, else ``default``."""
    data = _read()
    if name in data:
        return str(data[name])
    return os.environ.get(env_name or name.upper(), default)


def set_setting(name: str, value: Any) -> None:
    with _lock:
        data = _read()
        data[name] = value
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = f"{SETTINGS_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SETTINGS_FILE)
