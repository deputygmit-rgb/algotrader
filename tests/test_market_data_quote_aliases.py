import pytest

from src.api.market_data import MarketDataProvider
from src.data.models import Quote


@pytest.mark.asyncio
async def test_quote_model_accepts_al_and_time_high_aliases():
    q = Quote.model_validate({
        "instrument_token": 256265,
        "al": 152.35,
        "time_high": "2024-10-15 10:00:00",
        "last_price": 153.2,
        "average_price": 151.8,
        "ohlc": {"open": 149.5, "high": 154.0, "low": 148.2, "close": 150.8},
    })

    assert q.al == 152.35
    assert q.average_price == 151.8
    assert q.time_high == "2024-10-15 10:00:00"


@pytest.mark.asyncio
async def test_market_data_provider_includes_al_and_time_high_in_quote_rows():
    class DummyClient:
        async def get_quote(self, instruments):
            return {
                "NSE:INFY": Quote.model_validate({
                    "instrument_token": 256265,
                    "al": 152.35,
                    "time_high": "2024-10-15 10:00:00",
                    "last_price": 153.2,
                    "average_price": 151.8,
                    "ohlc": {"open": 149.5, "high": 154.0, "low": 148.2, "close": 150.8},
                    "volume": 1200,
                    "buy_quantity": 15,
                    "sell_quantity": 20,
                })
            }

    provider = MarketDataProvider(DummyClient())
    result = await provider.get_quote(["NSE:INFY"])

    quote = result["quotes"]["NSE:INFY"]
    assert quote["al"] == 152.35
    assert quote["average_price"] == 151.8
    assert quote["time_high"] == "2024-10-15 10:00:00"
