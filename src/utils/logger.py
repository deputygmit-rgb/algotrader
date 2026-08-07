from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

from src.utils.config import get_settings


def setup_logging() -> None:
    settings = get_settings()

    os.makedirs(os.path.dirname(settings.log_file), exist_ok=True)
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Route structlog events through stdlib logging so BOTH the console and the
    # log file receive them (structlog's PrintLoggerFactory used to only print
    # to stdout, silently dropping every structured event from logs/trading.log).
    # foreign_pre_chain adds level/timestamp to plain logging.getLogger(...) calls
    # too (several modules still log via stdlib logging directly, not get_logger()).
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
        ],
    )
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(settings.log_file, mode="a")
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(stdout_handler)
    root_logger.addHandler(file_handler)
    root_logger.setLevel(level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def sanitize_log_data(data: dict[str, Any]) -> dict[str, Any]:
    sensitive_keys = {"api_secret", "access_token", "request_token", "password", "secret"}
    sanitized = {}
    for key, value in data.items():
        if key.lower() in sensitive_keys:
            sanitized[key] = "***REDACTED***"
        elif isinstance(value, dict):
            sanitized[key] = sanitize_log_data(value)
        else:
            sanitized[key] = value
    return sanitized
