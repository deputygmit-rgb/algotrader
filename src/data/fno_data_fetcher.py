"""
F&O Data Fetcher — Fetch and store F&O data from Kite Connect API
===================================================================

Downloads F&O historical data when live trading is available:
  • Option chain snapshots with IV, Greeks
  • Underlying spot candles (NIFTY/BANKNIFTY index)
  • Futures contracts with OI

Data is stored in local SQLite database for offline backtesting.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, date, timedelta
from typing import Any, Optional, TYPE_CHECKING

from src.data.fno_data_store import FnODataStore, get_fno_data_store
from src.data.market_data_store import MarketDataStore, get_market_data_store
from src.data.models import Interval
from src.options.greeks import BlackScholes

if TYPE_CHECKING:
    from src.api.client import KiteClient

logger = logging.getLogger("fno_data_fetcher")

# Kite Index instrument tokens (well-known)
INDEX_TOKENS = {
    "NIFTY": 256265,
    "BANKNIFTY": 260105,
    "FINNIFTY": 257801,
    "MIDCPNIFTY": 288009,
    "NIFTYNXT50": 270857,
    "SENSEX": 265,
}

# Default lot sizes (may change, but these are common)
DEFAULT_LOT_SIZES = {
    "NIFTY": 50,
    "BANKNIFTY": 15,
    "FINNIFTY": 40,
    "MIDCPNIFTY": 75,
}


class FnODataFetcher:
    """Fetch F&O data from Kite and store locally."""
    
    def __init__(
        self,
        kite_client: Optional["KiteClient"] = None,
        fno_store: Optional[FnODataStore] = None,
        market_store: Optional[MarketDataStore] = None,
    ) -> None:
        self._kite = kite_client
        self._fno_store = fno_store or get_fno_data_store()
        self._market_store = market_store or get_market_data_store()
        self._rate_limiter_delay = 0.4  # 400ms between API calls
    
    # Risk-free rate for BS pricing (India ~7%)
    RISK_FREE_RATE = 0.07

    def set_kite_client(self, client: "KiteClient") -> None:
        """Set Kite client (for API calls)."""
        self._kite = client

    @staticmethod
    def _compute_greeks(
        spot: float,
        strike: float,
        option_type: str,
        market_price: float,
        expiry: str,
        rate: float = 0.07,
    ) -> dict[str, float]:
        """Compute IV and Greeks for a single option via Black-Scholes.

        Args:
            spot: Underlying spot price.
            strike: Option strike price.
            option_type: 'CE' or 'PE'.
            market_price: Last traded price of the option.
            expiry: Expiry date string 'YYYY-MM-DD'.
            rate: Risk-free rate (annualised).

        Returns:
            Dict with iv, delta, gamma, theta, vega.
        """
        zeros = {"iv": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        if spot <= 0 or strike <= 0 or market_price <= 0:
            return zeros

        try:
            expiry_date = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
            days_to_expiry = (expiry_date - date.today()).days
            if days_to_expiry <= 0:
                return zeros
            tte = days_to_expiry / 365.0
        except (ValueError, TypeError):
            return zeros

        try:
            iv = BlackScholes.implied_volatility(
                market_price=market_price,
                spot=spot,
                strike=strike,
                rate=rate,
                time_to_expiry=tte,
                option_type=option_type,
            )
            if iv <= 0.01 or iv >= 5.0:
                return zeros

            delta = BlackScholes.delta(spot, strike, rate, tte, iv, option_type)
            gamma = BlackScholes.gamma(spot, strike, rate, tte, iv)
            theta = BlackScholes.theta(spot, strike, rate, tte, iv, option_type)
            vega = BlackScholes.vega(spot, strike, rate, tte, iv)

            return {
                "iv": round(iv * 100, 2),      # store as percentage
                "delta": round(delta, 4),
                "gamma": round(gamma, 6),
                "theta": round(theta, 2),
                "vega": round(vega, 2),
            }
        except Exception:
            return zeros

    @property
    def is_available(self) -> bool:
        """Check if live data fetching is available."""
        return self._kite is not None and self._kite._auth.is_authenticated
    
    async def _rate_limit(self) -> None:
        """Apply rate limiting between API calls."""
        await asyncio.sleep(self._rate_limiter_delay)
    
    # ─── Underlying Candles ─────────────────────────────────────
    
    async def fetch_underlying_candles(
        self,
        underlying: str,
        from_date: date,
        to_date: date,
        interval: str = "minute",
    ) -> int:
        """Fetch underlying spot candles and store locally."""
        if not self.is_available:
            logger.warning("Kite client not available, cannot fetch candles")
            return 0
        
        token = INDEX_TOKENS.get(underlying.upper())
        if not token:
            logger.error("Unknown underlying: %s", underlying)
            return 0
        
        total_stored = 0
        status = "completed"
        
        try:
            # Kite limits historical data to 60 days per request for minute data
            current_from = from_date
            while current_from <= to_date:
                chunk_to = min(current_from + timedelta(days=59), to_date)
                
                logger.info(
                    "Fetching %s candles %s to %s (%s)",
                    underlying, current_from, chunk_to, interval,
                )
                
                await self._rate_limit()
                
                # Convert interval string to Interval enum
                try:
                    interval_enum = Interval(interval)
                except ValueError:
                    interval_enum = Interval.MINUTE
                
                candles = await self._kite.get_historical_data(
                    instrument_token=token,
                    interval=interval_enum,
                    from_date=current_from.isoformat(),
                    to_date=chunk_to.isoformat(),
                    continuous=False,
                    oi=False,
                )
                
                if candles:
                    # Convert to dict format
                    candle_dicts = [
                        {
                            "ts": c.timestamp,
                            "open": c.open,
                            "high": c.high,
                            "low": c.low,
                            "close": c.close,
                            "volume": c.volume,
                        }
                        for c in candles
                    ]
                    count = self._fno_store.store_underlying_candles(underlying.upper(), candle_dicts)
                    total_stored += count
                    logger.info("Stored %d candles for %s", count, underlying)
                
                current_from = chunk_to + timedelta(days=1)
            
        except Exception as e:
            logger.error("Error fetching candles: %s", e)
            status = "partial" if total_stored > 0 else "failed"
        
        # Log sync
        self._fno_store.log_sync(
            underlying=underlying.upper(),
            data_type="candles",
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            rows_stored=total_stored,
            interval=interval,
            status=status,
        )
        
        return total_stored
    
    # ─── Instrument Chain ───────────────────────────────────────
    
    def get_fno_instruments(
        self,
        underlying: str,
        expiry: str = "",
    ) -> list[dict[str, Any]]:
        """Get F&O instruments from cached instrument master."""
        instruments = self._market_store.get_instruments("NFO")
        
        # Filter by underlying
        ul = underlying.upper()
        filtered = [
            i for i in instruments
            if i.get("name") == ul or i.get("tradingsymbol", "").startswith(ul)
        ]
        
        # Filter by expiry if specified
        if expiry:
            exp_date = expiry if isinstance(expiry, str) else expiry.isoformat()
            filtered = [
                i for i in filtered
                if str(i.get("expiry", ""))[:10] == exp_date[:10]
            ]
        
        return filtered
    
    def get_fno_expiries(self, underlying: str) -> list[str]:
        """Get available F&O expiries from instrument master."""
        instruments = self.get_fno_instruments(underlying)
        expiries = set()
        for i in instruments:
            exp = i.get("expiry")
            if exp:
                exp_str = str(exp)[:10] if hasattr(exp, "isoformat") else str(exp)[:10]
                expiries.add(exp_str)
        return sorted(expiries)
    
    # ─── Option Chain Snapshot ──────────────────────────────────
    
    async def fetch_option_chain_snapshot(
        self,
        underlying: str,
        expiry: str,
        spot_price: float,
    ) -> int:
        """Fetch current option chain snapshot and store it."""
        if not self.is_available:
            logger.warning("Kite client not available")
            return 0
        
        # Get option instruments for this expiry
        instruments = self.get_fno_instruments(underlying, expiry)
        options = [
            i for i in instruments
            if i.get("instrument_type") in ("CE", "PE")
        ]
        
        if not options:
            logger.warning("No options found for %s expiry %s", underlying, expiry)
            return 0
        
        ts = datetime.now().isoformat()
        lot_size = DEFAULT_LOT_SIZES.get(underlying.upper(), 50)
        
        # Get LTP for all options
        symbols = [f"NFO:{o['tradingsymbol']}" for o in options]
        
        try:
            await self._rate_limit()
            ltp_data = await self._kite.get_ltp(symbols)
        except Exception as e:
            logger.error("Error fetching LTPs: %s", e)
            return 0
        
        # Build option chain data with Greeks computed via Black-Scholes
        option_data = []
        for opt in options:
            token = opt["instrument_token"]
            ltp_quote = ltp_data.get(f"NFO:{opt['tradingsymbol']}")
            last_price = ltp_quote.last_price if ltp_quote else 0
            opt_type = opt.get("instrument_type", "")
            strike = opt.get("strike", 0)

            greeks = self._compute_greeks(
                spot=spot_price,
                strike=strike,
                option_type=opt_type,
                market_price=last_price,
                expiry=expiry,
                rate=self.RISK_FREE_RATE,
            )

            option_data.append({
                "strike": strike,
                "option_type": opt_type,
                "last_price": last_price,
                "iv": greeks["iv"],
                "delta": greeks["delta"],
                "gamma": greeks["gamma"],
                "theta": greeks["theta"],
                "vega": greeks["vega"],
                "oi": 0,  # LTP endpoint does not return OI
                "oi_change": 0,
                "volume": 0,
                "bid_price": 0,
                "ask_price": 0,
                "instrument_token": token,
                "tradingsymbol": opt.get("tradingsymbol", ""),
                "lot_size": lot_size,
            })
        
        # Store
        count = self._fno_store.store_option_chain_snapshot(
            underlying=underlying.upper(),
            expiry=expiry,
            spot_price=spot_price,
            ts=ts,
            options=option_data,
        )
        
        logger.info("Stored %d option snapshots for %s %s", count, underlying, expiry)
        return count
    
    async def fetch_option_chain_with_quotes(
        self,
        underlying: str,
        expiry: str,
        spot_price: float,
    ) -> int:
        """Fetch option chain with full quotes (OI, volume, bid/ask)."""
        if not self.is_available:
            logger.warning("Kite client not available")
            return 0
        
        instruments = self.get_fno_instruments(underlying, expiry)
        options = [
            i for i in instruments
            if i.get("instrument_type") in ("CE", "PE")
        ]
        
        if not options:
            logger.warning("No options found for %s expiry %s", underlying, expiry)
            return 0
        
        ts = datetime.now().isoformat()
        lot_size = DEFAULT_LOT_SIZES.get(underlying.upper(), 50)
        
        # Batch quotes (max 500 per call)
        option_data = []
        batch_size = 200
        
        for i in range(0, len(options), batch_size):
            batch = options[i:i + batch_size]
            symbols = [f"NFO:{o['tradingsymbol']}" for o in batch]
            
            try:
                await self._rate_limit()
                quotes = await self._kite.get_quote(symbols)
            except Exception as e:
                logger.error("Error fetching quotes batch %d: %s", i // batch_size, e)
                continue
            
            for opt in batch:
                symbol = f"NFO:{opt['tradingsymbol']}"
                q = quotes.get(symbol)
                
                # Extract bid/ask from depth if available
                bid_price = q.depth.buy[0].price if q and q.depth.buy else 0
                ask_price = q.depth.sell[0].price if q and q.depth.sell else 0
                last_price = q.last_price if q else 0
                opt_type = opt.get("instrument_type", "")
                strike = opt.get("strike", 0)

                greeks = self._compute_greeks(
                    spot=spot_price,
                    strike=strike,
                    option_type=opt_type,
                    market_price=last_price,
                    expiry=expiry,
                    rate=self.RISK_FREE_RATE,
                )

                option_data.append({
                    "strike": strike,
                    "option_type": opt_type,
                    "last_price": last_price,
                    "iv": greeks["iv"],
                    "delta": greeks["delta"],
                    "gamma": greeks["gamma"],
                    "theta": greeks["theta"],
                    "vega": greeks["vega"],
                    "oi": q.oi if q else 0,
                    "oi_change": 0,  # Not available in Quote model
                    "volume": q.volume if q else 0,
                    "bid_price": bid_price,
                    "ask_price": ask_price,
                    "instrument_token": opt.get("instrument_token", 0),
                    "tradingsymbol": opt.get("tradingsymbol", ""),
                    "lot_size": lot_size,
                })
        
        # Store
        count = self._fno_store.store_option_chain_snapshot(
            underlying=underlying.upper(),
            expiry=expiry,
            spot_price=spot_price,
            ts=ts,
            options=option_data,
        )
        
        self._fno_store.log_sync(
            underlying=underlying.upper(),
            data_type="option_chain",
            from_date=ts[:10],
            to_date=ts[:10],
            rows_stored=count,
            status="completed",
        )
        
        logger.info("Stored %d option quotes for %s %s", count, underlying, expiry)
        return count
    
    # ─── Historical Option Candles ──────────────────────────────
    
    async def fetch_option_candles(
        self,
        instrument_token: int,
        from_date: date,
        to_date: date,
        interval: str = "minute",
    ) -> list[dict[str, Any]]:
        """Fetch historical candles for a specific option instrument."""
        if not self.is_available:
            return []
        
        candles = []
        current_from = from_date
        
        while current_from <= to_date:
            chunk_to = min(current_from + timedelta(days=59), to_date)
            
            try:
                await self._rate_limit()
                
                # Convert interval string to Interval enum
                try:
                    interval_enum = Interval(interval)
                except ValueError:
                    interval_enum = Interval.MINUTE
                
                raw = await self._kite.get_historical_data(
                    instrument_token=instrument_token,
                    interval=interval_enum,
                    from_date=current_from.isoformat(),
                    to_date=chunk_to.isoformat(),
                    continuous=False,
                    oi=True,
                )
                
                for c in raw:
                    candles.append({
                        "ts": c.timestamp,
                        "open": c.open,
                        "high": c.high,
                        "low": c.low,
                        "close": c.close,
                        "volume": c.volume,
                        "oi": c.oi or 0,
                    })
                
            except Exception as e:
                logger.error("Error fetching option candles: %s", e)
            
            current_from = chunk_to + timedelta(days=1)
        
        return candles
    
    # ─── Bulk Download ──────────────────────────────────────────
    
    async def download_fno_data(
        self,
        underlying: str,
        from_date: date,
        to_date: date,
        interval: str = "minute",
        include_options: bool = True,
        expiry: str = "",
        on_progress: Optional[callable] = None,
    ) -> dict[str, int]:
        """Download comprehensive F&O data for backtesting."""
        results = {
            "underlying_candles": 0,
            "option_chains": 0,
            "futures": 0,
        }
        
        # 1. Fetch underlying candles
        if on_progress:
            on_progress("Fetching underlying candles...")
        
        results["underlying_candles"] = await self.fetch_underlying_candles(
            underlying, from_date, to_date, interval
        )
        
        # 2. Fetch instrument master if needed
        if include_options:
            if on_progress:
                on_progress("Refreshing instrument master...")
            
            try:
                await self._rate_limit()
                instruments = await self._kite.get_instruments("NFO")
                if instruments:
                    # Convert Instrument models to dicts for storage
                    instrument_dicts = [inst.model_dump() for inst in instruments]
                    self._market_store.store_instruments("NFO", instrument_dicts)
                    logger.info("Stored %d NFO instruments", len(instruments))
            except Exception as e:
                logger.error("Error fetching instruments: %s", e)
        
        # 3. Fetch option chain snapshot for current state
        if include_options and self.is_available:
            if on_progress:
                on_progress("Fetching option chain snapshots...")
            
            # Get spot price
            try:
                ul = underlying.upper()
                token = INDEX_TOKENS.get(ul)
                if token:
                    ltp_result = await self._kite.get_ltp([f"NSE:{ul}"])
                    ltp_quote = ltp_result.get(f"NSE:{ul}")
                    spot = ltp_quote.last_price if ltp_quote else 0
                    
                    # Get expiries to fetch
                    expiries = [expiry] if expiry else self.get_fno_expiries(ul)[:3]
                    
                    for exp in expiries:
                        if on_progress:
                            on_progress(f"Fetching {ul} {exp} option chain...")
                        
                        count = await self.fetch_option_chain_with_quotes(ul, exp, spot)
                        results["option_chains"] += count
                        
            except Exception as e:
                logger.error("Error fetching option chains: %s", e)
        
        if on_progress:
            on_progress("Download complete!")
        
        return results
    
    # ─── Data Coverage ──────────────────────────────────────────
    
    def get_coverage(self, underlying: str = "") -> dict[str, Any]:
        """Get data coverage summary."""
        if underlying:
            return self._fno_store.get_data_coverage(underlying)
        else:
            stats = self._fno_store.get_stats()
            coverage = {}
            for ul in stats.get("underlyings", []):
                coverage[ul] = self._fno_store.get_data_coverage(ul)
            return {
                "stats": stats,
                "coverage": coverage,
            }


# ─── Singleton ──────────────────────────────────────────────────

_fetcher_instance: Optional[FnODataFetcher] = None

def get_fno_data_fetcher() -> FnODataFetcher:
    """Get or create singleton FnO data fetcher."""
    global _fetcher_instance
    if _fetcher_instance is None:
        _fetcher_instance = FnODataFetcher()
    return _fetcher_instance
