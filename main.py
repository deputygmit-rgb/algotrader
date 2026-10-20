from __future__ import annotations

import os

import uvicorn

from src.utils.config import get_settings
from src.utils.clock import use_exchange_timezone
from src.utils.logger import setup_logging


def main() -> None:
    use_exchange_timezone()
    setup_logging()

    port = int(os.environ.get("PORT", 5001))
    host = "0.0.0.0"

    uvicorn.run(
        "src.api.webapp:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
