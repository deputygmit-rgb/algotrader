"""Shared helpers for making computed results (numpy/pandas-heavy) JSON-safe."""
from __future__ import annotations

from typing import Any

import numpy as np


def sanitize_for_json(obj: Any) -> Any:
    """Recursively convert numpy scalar types (and NaN floats) to native Python
    types so the result can be safely returned from a FastAPI JSON endpoint.
    """
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_json(v) for v in obj]
    if isinstance(obj, (np.bool_, np.generic)):
        val = obj.item()
        if isinstance(val, float) and val != val:  # NaN
            return None
        return val
    if isinstance(obj, float) and obj != obj:  # NaN
        return None
    return obj
