from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional

from src.data.models import TradeJournalEntry
from src.utils.config import get_settings
from src.utils.logger import get_logger

logger = get_logger(__name__)


class TradeJournal:
    def __init__(self, path: Optional[str] = None) -> None:
        self._settings = get_settings()
        self._path = path or self._settings.trade_journal_file
        self._entries: list[TradeJournalEntry] = []
        self._pnl_tracker: dict[str, float] = {}
        self._load_journal()

    def record_trade(
        self,
        strategy: str,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        price: float,
        order_id: str,
        status: str,
        pnl: float = 0.0,
        notes: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        entry = TradeJournalEntry(
            timestamp=datetime.now(),
            strategy=strategy,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            transaction_type=transaction_type,
            quantity=quantity,
            price=price,
            order_id=order_id,
            status=status,
            pnl=pnl,
            notes=notes,
            metadata=metadata or {},
        )
        self._entries.append(entry)

        key = f"{exchange}:{tradingsymbol}"
        self._pnl_tracker[key] = self._pnl_tracker.get(key, 0.0) + pnl

        self._save_journal()
        logger.info(
            "trade_recorded",
            strategy=strategy,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            price=price,
            pnl=pnl,
        )

    def get_daily_pnl(self) -> float:
        today = datetime.now().date()
        return sum(
            e.pnl for e in self._entries if e.timestamp.date() == today
        )

    def get_total_pnl(self) -> float:
        return sum(e.pnl for e in self._entries)

    def get_pnl_by_instrument(self) -> dict[str, float]:
        return self._pnl_tracker.copy()

    def get_pnl_by_strategy(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for entry in self._entries:
            result[entry.strategy] = result.get(entry.strategy, 0.0) + entry.pnl
        return result

    def get_trade_count(self) -> int:
        return len(self._entries)

    def get_entries(
        self,
        strategy: str | None = None,
        tradingsymbol: str | None = None,
        limit: int = 100,
    ) -> list[TradeJournalEntry]:
        filtered = self._entries
        if strategy:
            filtered = [e for e in filtered if e.strategy == strategy]
        if tradingsymbol:
            filtered = [e for e in filtered if e.tradingsymbol == tradingsymbol]
        return filtered[-limit:]

    def get_summary(self) -> dict[str, Any]:
        return {
            "total_trades": len(self._entries),
            "total_pnl": self.get_total_pnl(),
            "daily_pnl": self.get_daily_pnl(),
            "pnl_by_instrument": self.get_pnl_by_instrument(),
            "pnl_by_strategy": self.get_pnl_by_strategy(),
        }

    def _save_journal(self) -> None:
        try:
            path = self._path
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = [e.model_dump(mode="json") for e in self._entries]
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except OSError as e:
            logger.error("journal_save_error", error=str(e))

    def _load_journal(self) -> None:
        path = self._path
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self._entries = [TradeJournalEntry(**e) for e in data]
            for entry in self._entries:
                key = f"{entry.exchange}:{entry.tradingsymbol}"
                self._pnl_tracker[key] = self._pnl_tracker.get(key, 0.0) + entry.pnl
            logger.info("journal_loaded", entries=len(self._entries))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("journal_load_error", error=str(e))
