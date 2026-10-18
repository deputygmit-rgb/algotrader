from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.api.service import TradingService


@pytest.mark.asyncio
async def test_test_stock_strategy_uses_dma200_warmup_for_trend_strategies() -> None:
    service = TradingService()
    service.resolve_instrument_token = AsyncMock(return_value=12345)
    service.import_historical_data = AsyncMock()

    rows = []
    ts = datetime(2024, 1, 1, 9, 15)
    for i in range(120):
        rows.append({
            "ts": (ts + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S"),
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "volume": 1000 + i,
        })
    service._resolve_ohlcv_rows = AsyncMock(return_value=rows)

    with patch("src.api.service.BacktestEngine.run", return_value={
        "win_rate": 60.0,
        "sharpe_ratio": 1.5,
        "max_drawdown_pct": 12.0,
        "total_return_pct": 15.0,
        "total_trades": 12,
        # profitable trades in the out-of-sample (last 30%) segment
        "trades": [{"entry_time": "2099-01-01 09:15:00", "exit_bar": 1, "pnl": 100.0}] * 3,
    }):
        result = await service.test_stock_strategy("RELIANCE", "golden_death_cross")

    assert result["overall_passed"] is True
    assert service.import_historical_data.await_args_list[0].kwargs["days"] == 107
    assert service._resolve_ohlcv_rows.await_args_list[0].kwargs["days"] == 107
