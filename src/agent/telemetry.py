"""In-process telemetry bus: TradingService publishes what it does, agents subscribe.

Producers call ``emit("strategy.eval", strategy=..., symbol=...)`` at the points
where work happens (bar close, strategy evaluation, signal, order, scan
progress). Subscribers register a topic prefix (``"strategy."``, ``"order."``,
``""`` for everything) and receive ``(topic, data)`` synchronously. Callbacks
must be cheap and must not raise into the producer; failures are logged and
swallowed so observability can never break trading.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from src.utils.logger import get_logger

logger = get_logger(__name__)

Subscriber = Callable[[str, dict[str, Any]], None]


class Telemetry:
    def __init__(self) -> None:
        self._subs: dict[str, list[Subscriber]] = defaultdict(list)

    def subscribe(self, prefix: str, fn: Subscriber) -> None:
        self._subs[prefix].append(fn)

    def unsubscribe(self, fn: Subscriber) -> None:
        for subs in self._subs.values():
            if fn in subs:
                subs.remove(fn)

    def emit(self, topic: str, **data: Any) -> None:
        for prefix, subs in list(self._subs.items()):
            if not topic.startswith(prefix):
                continue
            for fn in list(subs):
                try:
                    fn(topic, data)
                except Exception as e:   # observability must never break the producer
                    logger.error("telemetry_subscriber_failed", topic=topic, error=str(e))
