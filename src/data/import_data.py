import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

"""
Data Importer for Kite Connect
=============================

Fetches and stores all available historical and live data from Kite Connect,
organized into two categories: equity and FNO (derivatives).
Data is saved in a structured format for efficient usage in backtest and paper trading engines.
"""

import os
import asyncio
import pandas as pd
from datetime import datetime, timedelta
from src.api.client import KiteClient
from src.api.market_data import MarketDataProvider
from src.data.market_data_store import MarketDataStore
from src.data.models import Interval
from src.utils.logger import get_logger

logger = get_logger(__name__)

DATA_DIR = "data/imported"
EQUITY_DIR = os.path.join(DATA_DIR, "equity")
FNO_DIR = os.path.join(DATA_DIR, "fno")
os.makedirs(EQUITY_DIR, exist_ok=True)
os.makedirs(FNO_DIR, exist_ok=True)

async def fetch_and_store(symbol: str, exchange: str, interval: str, start: datetime, end: datetime, category: str):
    client = KiteClient()
    store = MarketDataStore()
    provider = MarketDataProvider(client, store)
    df = await provider.get_historical_ohlcv(symbol, exchange, interval, start, end)
    if df is not None and not df.empty:
        out_dir = EQUITY_DIR if category == "equity" else FNO_DIR
        out_path = os.path.join(out_dir, f"{symbol}_{interval}.parquet")
        df.to_parquet(out_path)
        logger.info(f"Saved {category} data: {out_path} ({len(df)} rows)")
    else:
        logger.warning(f"No data for {symbol} ({category}) in {interval}")

async def fetch_and_store_to_db(symbol: str, exchange: str, interval: str, start: datetime, end: datetime, category: str):
    client = KiteClient()
    store = MarketDataStore()
    provider = MarketDataProvider(client, store)
    instrument_token = await provider.resolve_token(symbol, exchange)
    if instrument_token is None:
        logger.warning(f"Could not resolve token for {symbol} ({category})")
        return
    # Fetch historical chart data
    chart = await provider.get_historical_chart(
        instrument_token,
        interval=interval,
        from_date=start.strftime("%Y-%m-%d"),
        to_date=end.strftime("%Y-%m-%d"),
        days=(end - start).days
    )
    if not chart or 'candles' not in chart or not chart['candles']:
        logger.warning(f"No data for {symbol} ({category}) in {interval}")
        return
    # Save each candle to DB
    for row in chart['candles']:
        store._get_conn().execute(
            """
            INSERT OR REPLACE INTO candles (instrument_token, interval, ts, open, high, low, close, volume, oi)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instrument_token,
                interval,
                row['timestamp'],
                row['open'],
                row['high'],
                row['low'],
                row['close'],
                int(row['volume']) if 'volume' in row else 0,
                row.get('oi'),
            )
        )
    store._get_conn().commit()
    logger.info(f"Saved {category} data for {symbol} to DB: {len(chart['candles'])} rows")

async def import_all_data(equity_symbols, fno_symbols, interval="day", days=365):
    end = datetime.now()
    start = end - timedelta(days=days)
    tasks = []
    for symbol in equity_symbols:
        tasks.append(fetch_and_store(symbol, "NSE", interval, start, end, "equity"))
    for symbol in fno_symbols:
        tasks.append(fetch_and_store(symbol, "NFO", interval, start, end, "fno"))
    await asyncio.gather(*tasks)

async def import_all_data_to_db(equity_symbols, fno_symbols, interval="day", days=365):
    end = datetime.now()
    start = end - timedelta(days=days)
    tasks = []
    for symbol in equity_symbols:
        tasks.append(fetch_and_store_to_db(symbol, "NSE", interval, start, end, "equity"))
    for symbol in fno_symbols:
        tasks.append(fetch_and_store_to_db(symbol, "NFO", interval, start, end, "fno"))
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    # Example usage: customize these lists as needed
    equity_symbols = ["RELIANCE", "TCS", "INFY"]
    fno_symbols = ["NIFTY", "BANKNIFTY"]
    asyncio.run(import_all_data_to_db(equity_symbols, fno_symbols, interval="day", days=730))
