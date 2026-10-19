from __future__ import annotations

import asyncio
import json
import re
import os
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from src.analysis.scanner import StockScanner
from src.analysis.indicators import compute_all_indicators
from src.analysis.nifty_contribution import compute_nifty_contribution, nifty_quote_instruments
from src.analysis.fno_flow import FlowHistory, build_universe, compute_fno_flow, quote_keys
from src.api.day_trade import DayTradeTracker, market_open
from src.analysis.nifty500 import NIFTY_500_SYMBOLS, get_sector
from src.agent.telemetry import Telemetry
from src.api.client import KiteClient
from src.api.market_data import MarketDataProvider, PortfolioReporter, _resolve_holding_quantity
from src.api.ticker import KiteTicker
from src.auth.authenticator import KiteAuthenticator
from src.data.backtest import BacktestEngine, WalkForwardOptimizer
from src.data.bar_aggregator import BarAggregator, bucket_start
from src.data.exit_plan import TIMEFRAME_MINUTES, allows_short, product_for
from src.data.journal import TradeJournal
from src.data.market_data_store import get_market_data_store
from src.data.models import (
    Exchange,
    Interval,
    OrderType,
    ProductType,
    Signal,
    Tick,
    TickMode,
    TransactionType,
)
from src.data.paper_trader import PaperTradingEngine
from src.derivatives.fno_backtest import FnOBacktestEngine
from src.derivatives.fno_paper_trader import FnOPaperTradingEngine
from src.data.fno_data_fetcher import INDEX_TOKENS
from src.utils.serialization import sanitize_for_json
from src.execution.engine import ExecutionEngine
from src.journal.journal_store import JournalStore
from src.journal.journal_analytics import JournalAnalytics
from src.journal.portfolio_health import PortfolioHealthTracker
from src.journal.journal_models import (
    JournalEntry, ExecutionRecord, StrategyContext,
    PositionStructure, CostBreakdown, ExcursionMetrics,
    PortfolioSnapshot, CapitalMetrics, RiskMetrics, SystemEvent,
    SignalQuality, LiquiditySnapshot,
)
from src.options.chain import OptionChainBuilder
from src.options.oi_analysis import (
    FuturesOIAnalyzer,
    OptionsOIAnalyzer,
    futures_report_has_quotes,
    options_report_has_market_data,
)
from src.options.oi_tracker import NIFTY_TOKEN, SENSEX_TOKEN, OITracker
from src.options.oi_strategy import OIStrategyEngine, OIStrategyConfig
from src.options.oi_fno_bridge import OIFnOBridge
from src.options.strategies import (
    BearPutSpreadStrategy,
    BullCallSpreadStrategy,
    IronCondorStrategy,
    OptionStrategyBase,
    StraddleStrangleStrategy,
)
from src.risk.manager import RiskManager
from src.strategy.base import BaseStrategy
from src.strategy.custom_builder import (
    CustomStrategy,
    CustomStrategyStore,
    get_available_indicators,
)
from src.strategy.ema_crossover import EMACrossoverStrategy
from src.strategy.mean_reversion import MeanReversionStrategy
from src.strategy.rsi_strategy import RSIStrategy
from src.strategy.scanner_strategy import ScannerStrategy
from src.strategy.day_trade_setups import DayTradeSetupsStrategy
from src.strategy.vwap_breakout import VWAPBreakoutStrategy
from src.strategy.health_engine import compute_health_report, HealthReport, PillarResult, get_health_grade
from src.options.fno_builder import (
    FnOStrategyStore,
    FnOStrategyConfig,
    FnOLeg,
    resolve_legs,
    compute_strategy_payoff,
    UNDERLYING_LOT_SIZES,
    UNDERLYING_STRIKE_GAPS,
)
from src.utils.config import get_settings
from src.utils.exceptions import KillSwitchError, RiskLimitError
from src.utils.logger import get_logger
from src.utils.user_paths import user_data_path

import pandas as pd

logger = get_logger(__name__)


class TradingService:
    _instance: Optional[TradingService] = None

    def __init__(self, api_key: str = "", api_secret: str = "", zerodha_id: str = "") -> None:
        self._settings = get_settings()
        self._zerodha_id = zerodha_id
        self._auth = KiteAuthenticator(api_key=api_key, api_secret=api_secret, zerodha_id=zerodha_id)
        self._client = KiteClient(self._auth)
        self._ticker = KiteTicker(self._auth)
        self._risk = RiskManager()
        self._execution = ExecutionEngine(self._client, self._risk)
        # Journals and saved strategies are per user (see src/utils/user_paths.py).
        self._journal = TradeJournal(
            user_data_path(zerodha_id, "trade_journal.json", self._settings.trade_journal_file)
        )
        self._journal_store = JournalStore(
            db_path=user_data_path(zerodha_id, "journal.db", "data/journal.db")
        )
        self._journal_analytics = JournalAnalytics(self._journal_store)
        self._portfolio_health = PortfolioHealthTracker(self._journal_store)
        
        # Wire execution engine → journal callbacks
        self._execution.set_on_fill_callback(self._on_execution_fill)
        self._execution.set_on_exit_callback(self._on_execution_exit)
        self._chain_builder = OptionChainBuilder()
        self._oi_tracker = OITracker()
        self._oi_streaming = False
        self._futures_oi = FuturesOIAnalyzer()
        self._futures_oi_scan_lock = asyncio.Lock()
        self._options_oi = OptionsOIAnalyzer()
        self._oi_strategy = OIStrategyEngine()
        self._oi_fno_bridge = OIFnOBridge()
        self._scanner = StockScanner()
        # Analysis survives restarts: reload the last snapshot (stocks analysed
        # today are served instantly; older ones refresh on first use).
        self._snapshot_saved_mono = 0.0
        self._snapshot_dirty = False
        self._refresh_tasks: dict[str, asyncio.Task] = {}   # background re-analysis, one per symbol
        try:
            self._scanner.load_snapshot(self._analysis_snapshot_path())
        except Exception as e:
            logger.warning("scanner_snapshot_load_failed", error=str(e))
        self._strategies: list[BaseStrategy] = []
        # Live strategies run on completed bars built from ticks, each on its own
        # timeframe (see live_interval) so live matches the bars it was tested on.
        self._bar_aggs: dict[str, BarAggregator] = {}
        self._live_bars: dict[tuple[str, int], pd.DataFrame] = {}
        self._instruments_cache: dict[str, list] = {}
        self._running = False
        self._subscribed_tokens: list[int] = []
        self._live_ticks: dict[int, dict[str, Any]] = {}
        self._last_tick_at: Optional[float] = None   # wall-clock time of the last tick batch (feed health)
        self._tick_count = 0
        self._signal_log: list[dict[str, Any]] = []
        self._ws_clients: list[Any] = []
        self._lock = asyncio.Lock()
        self._custom_store = CustomStrategyStore(
            user_data_path(zerodha_id, "custom_strategies.json", "data/custom_strategies.json")
        )
        self._fno_strategy_store = FnOStrategyStore(
            user_data_path(zerodha_id, "fno_custom_strategies.json", "data/fno_custom_strategies.json")
        )
        self._paper_results: dict[str, Any] = {}  # cache latest paper trade results
        self._market_data = MarketDataProvider(self._client)
        self._portfolio_reporter = PortfolioReporter(self._client)
        self._backtest_results: dict[str, Any] = {}  # cache latest backtest results
        self._last_health_report: Optional[dict[str, Any]] = None  # cache latest health report
        self._market_store = get_market_data_store()
        self._last_quote_persist: dict[int, float] = {}
        self._quote_persist_interval_s = 2.0  # throttle per-token quote_snapshots writes
        self._oi_snapshot_task: Optional[asyncio.Task] = None
        # Workload events for the agents' logs (strategy evaluations, signals,
        # orders, scan progress). See src/agent/telemetry.py.
        self.telemetry = Telemetry()
        self._replay_task: Optional[asyncio.Task] = None
        self._replay_state: dict[str, Any] = {"status": "idle", "running": False}
        self._nifty_contrib_cache: tuple[float, dict[str, Any]] | None = None
        self._nifty_contrib_lock = asyncio.Lock()
        self._fno_universe: list[Any] = []
        self._fno_universe_date: str = ""
        self._fno_baselines: dict[str, dict[str, Any]] = {}
        self._fno_baseline_task: Optional[asyncio.Task] = None
        self._fno_cache: tuple[float, dict[str, Any]] | None = None
        self._fno_lock = asyncio.Lock()
        self._fno_history = FlowHistory()
        self._day_trade = DayTradeTracker(self._fetch_day_bars)
        self._day_trade_task: Optional[asyncio.Task] = None
        self._day_trade_last_request = 0.0
        # board persistence, signal journal and opt-in auto-trade (src/api/board_trader.py)
        from src.api.board_trader import BoardTrader

        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self._board = BoardTrader(self._emit, os.path.join(base, "data", "day_trade", self._zerodha_id or "default"))
        self._board_preflight: Any = None
        # per-minute OI-bias history (own analyser so manual scans aren't disturbed)
        self._oi_bias_analyzer = OptionsOIAnalyzer()
        self._oi_bias_task: Optional[asyncio.Task] = None
        self.last_oi_bias: dict[str, dict[str, Any]] = {}
        try:
            self._day_trade.load_state(self._board.load(datetime.now()), datetime.now())
        except Exception as e:
            logger.warning("board_restore_failed", error=str(e))

    @staticmethod
    def _task_exception_handler(task: asyncio.Task) -> None:
        """Log exceptions from fire-and-forget background tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("background_task_failed", task=task.get_name(), error=str(exc), exc_info=exc)

    @classmethod
    def get_instance(cls) -> TradingService:
        if cls._instance is None:
            cls._instance = TradingService()
        return cls._instance

    @property
    def zerodha_id(self) -> str:
        return self._zerodha_id

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_authenticated(self) -> bool:
        try:
            return self._auth.is_authenticated
        except Exception:
            return False

    def get_login_url(self) -> str:
        return self._auth.get_login_url()

    async def authenticate(self, request_token: str) -> dict[str, Any]:
        session = await self._auth.generate_session(request_token)
        return {"user_id": session.user_id, "user_name": session.user_name}

    async def search_instruments(self, query: str, exchange: str = "NSE") -> list[dict[str, Any]]:
        if exchange not in self._instruments_cache:
            instruments = await self._client.get_instruments(exchange)
            self._instruments_cache[exchange] = instruments

        results = []
        q = query.upper()
        eq_only = exchange in ("NSE", "BSE")
        for inst in self._instruments_cache[exchange]:
            if eq_only and inst.instrument_type not in ("EQ", ""):
                continue
            if not q or q in inst.tradingsymbol.upper() or q in (inst.name or "").upper():
                results.append({
                    "token": inst.instrument_token,
                    "symbol": inst.tradingsymbol,
                    "name": inst.name or inst.tradingsymbol,
                    "exchange": inst.exchange,
                    "type": inst.instrument_type,
                    "lot_size": inst.lot_size,
                    "expiry": inst.expiry or "",
                    "strike": inst.strike,
                })
            if len(results) >= 50:
                break
        return results

    async def get_profile(self) -> dict[str, Any]:
        profile = await self._client.get_profile()
        return profile.model_dump()

    async def get_margins(self) -> dict[str, Any]:
        margins = await self._client.get_margins()
        return margins.model_dump()

    async def get_orders(self) -> list[dict[str, Any]]:
        orders = await self._client.get_orders()
        return [o.model_dump() for o in orders]

    async def get_positions(self) -> dict[str, Any]:
        positions = await self._client.get_positions()
        return positions.model_dump()

    async def get_trades_today_count(self) -> int:
        """Executed trades in today's Zerodha tradebook (Kite returns today's only)."""
        return len(await self._client.get_trades())

    async def get_holdings(self) -> list[dict[str, Any]]:
        holdings = await self._client.get_holdings()
        return [{**h.model_dump(), "total_quantity": _resolve_holding_quantity(h)} for h in holdings]

    def get_risk_summary(self) -> dict[str, Any]:
        return self._risk.get_risk_summary()

    def get_journal_summary(self) -> dict[str, Any]:
        return self._journal.get_summary()

    # ── New 3-Layer Journal API ──

    def get_pro_journal_summary(self, source: str = "", days: int = 0,
                                 strategy: str = "", instrument: str = "",
                                 direction: str = "", trade_type: str = "") -> dict[str, Any]:
        return self._journal_store.get_summary(
            source=source, days=days, strategy=strategy,
            instrument=instrument, direction=direction, trade_type=trade_type)

    def get_journal_analytics(self, strategy: str = "", source: str = "",
                               days: int = 0, instrument: str = "",
                               direction: str = "", trade_type: str = "") -> dict[str, Any]:
        return self._journal_analytics.compute_full_analytics(
            strategy=strategy, source=source, days=days,
            instrument=instrument, direction=direction, trade_type=trade_type)

    def get_fno_journal_analytics(self, strategy: str = "",
                                   days: int = 0, instrument: str = "",
                                   direction: str = "", trade_type: str = "",
                                   source: str = "") -> dict[str, Any]:
        return self._journal_analytics.compute_fno_analytics(
            strategy=strategy, days=days)

    def get_journal_entries(self, strategy: str = "", instrument: str = "",
                            trade_type: str = "", source: str = "",
                            direction: str = "", review_status: str = "",
                            is_closed: bool | None = None,
                            from_date: str = "", to_date: str = "",
                            limit: int = 100, offset: int = 0) -> dict:
        entries = self._journal_store.query_entries(
            strategy=strategy, instrument=instrument, trade_type=trade_type,
            source=source, direction=direction, review_status=review_status,
            is_closed=is_closed,
            from_date=from_date, to_date=to_date,
            limit=limit, offset=offset)
        total = self._journal_store.count_entries(
            strategy=strategy, instrument=instrument, source=source,
            direction=direction, trade_type=trade_type,
            review_status=review_status,
            from_date=from_date, to_date=to_date)
        return {"entries": [e.to_dict() for e in entries], "total": total}

    def get_journal_entry(self, entry_id: str) -> dict[str, Any] | None:
        entry = self._journal_store.get_entry(entry_id)
        return entry.to_dict() if entry else None

    def get_regime_matrix(self, strategy: str = "", instrument: str = "",
                           source: str = "", direction: str = "",
                           trade_type: str = "", days: int = 0) -> list[dict]:
        return self._journal_store.get_regime_performance_matrix(
            strategy=strategy, instrument=instrument, source=source,
            direction=direction, trade_type=trade_type, days=days)

    def get_slippage_drift(self, days: int = 30, strategy: str = "",
                            instrument: str = "", source: str = "",
                            direction: str = "", trade_type: str = "") -> dict:
        return self._journal_store.get_slippage_drift(
            window_days=days, strategy=strategy, instrument=instrument,
            source=source, direction=direction, trade_type=trade_type)

    def get_daily_pnl_history(self, days: int = 30, strategy: str = "",
                               instrument: str = "", source: str = "",
                               direction: str = "", trade_type: str = "") -> list[dict]:
        return self._journal_store.get_pnl_by_date(
            days=days, strategy=strategy, instrument=instrument,
            source=source, direction=direction, trade_type=trade_type)

    def get_strategy_breakdown(self, source: str = "", days: int = 0,
                                instrument: str = "", direction: str = "",
                                trade_type: str = "", strategy: str = "") -> list[dict]:
        return self._journal_store.get_pnl_by_strategy(
            source=source, days=days, instrument=instrument,
            direction=direction, trade_type=trade_type, strategy=strategy)

    def get_portfolio_health(self) -> dict[str, Any]:
        return self._portfolio_health.get_current_health()

    def get_portfolio_health_history(self, days: int = 30) -> dict[str, Any]:
        return self._portfolio_health.get_health_summary(days=days)

    def get_equity_curve(self, days: int = 365) -> list[dict]:
        return self._journal_store.get_equity_curve(days=days)

    def get_system_events(self, event_type: str = "",
                           limit: int = 100) -> list[dict]:
        events = self._journal_store.get_system_events(
            event_type=event_type, limit=limit)
        return [e.to_dict() for e in events]

    def get_journal_db_stats(self) -> dict:
        return self._journal_store.get_db_stats()

    def get_journal_strategies(self) -> list[str]:
        return self._journal_store.get_strategies()

    def get_journal_instruments(self) -> list[str]:
        return self._journal_store.get_instruments()

    def record_journal_entry(self, entry_data: dict) -> str:
        """Record a new journal entry (from UI or integrations)."""
        entry = JournalEntry.from_dict(entry_data)
        return self._journal_store.record_entry(entry)

    def update_journal_entry(self, entry_id: str, updates: dict) -> bool:
        """Update an existing journal entry (notes, tags, review status)."""
        entry = self._journal_store.get_entry(entry_id)
        if not entry:
            return False
        if "notes" in updates:
            entry.notes = updates["notes"]
        if "tags" in updates:
            entry.tags = updates["tags"]
        if "review_status" in updates:
            entry.review_status = updates["review_status"]
        self._journal_store.record_entry(entry)
        return True

    def export_journal(self) -> list[dict]:
        return self._journal_store.export_all()

    def get_recent_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._signal_log[-limit:]

    def get_strategies_info(self) -> list[dict[str, Any]]:
        return [
            {
                "name": s.name,
                "is_active": s.is_active,
                "params": s.params,
                "type": "option" if isinstance(s, OptionStrategyBase) else "equity",
            }
            for s in self._strategies
        ]

    def get_execution_summary(self) -> dict[str, Any]:
        return self._execution.get_execution_summary()

    def get_live_ticks(self) -> dict[int, dict[str, Any]]:
        return self._live_ticks.copy()

    async def get_option_chain(
        self, underlying: str = "NIFTY", expiry: Optional[str] = None
    ) -> dict[str, Any]:
        cached = self._chain_builder.get_cached_chain(underlying, expiry or "")
        if cached:
            return cached.model_dump()
        return {"error": "No option chain data available. Subscribe to live data first."}

    @property
    def oi_tracker(self) -> OITracker:
        return self._oi_tracker

    def get_oi_summary(self, underlying: str = "NIFTY") -> dict[str, Any]:
        return self._oi_tracker.get_summary(underlying).model_dump()

    def get_oi_all(self) -> dict[str, Any]:
        return self._oi_tracker.get_all_tracked_data()

    async def get_futures_oi_report(self) -> dict[str, Any]:
        """Refresh futures OI, retaining the last usable report on refresh failure."""
        observed_report = self._futures_oi.get_last_report()
        async with self._futures_oi_scan_lock:
            cached = self._futures_oi.get_last_report()
            if cached is not None and cached is not observed_report:
                return {**cached.model_dump(), "cache_state": "cached"}

            try:
                report = await self._futures_oi.analyze(self._client)
                if not futures_report_has_quotes(report):
                    warning = "Futures OI refresh returned no usable quotes."
                    logger.warning("futures_oi_refresh_unusable", cached_report=bool(cached))
                    if cached:
                        return {
                            **cached.model_dump(),
                            "cache_state": "stale",
                            "refresh_warning": warning,
                        }
                    return {"error": warning + " No previous report is cached."}
                return {**report.model_dump(), "cache_state": "updated"}
            except Exception as e:
                logger.error("futures_oi_analysis_error", error=str(e))
                if cached:
                    return {
                        **cached.model_dump(),
                        "cache_state": "stale",
                        "refresh_warning": f"Refresh failed: {e}",
                    }
                return {"error": str(e)}

    def get_futures_oi_cached(self) -> dict[str, Any]:
        """Return last computed futures OI report."""
        report = self._futures_oi.get_last_report()
        return {**report.model_dump(), "cache_state": "cached"} if report else {"error": "No data yet. Run scan first."}

    async def get_options_oi_report(self, underlying: str = "NIFTY", expiry: Optional[str] = None) -> dict[str, Any]:
        """Refresh options OI, retaining the last usable report on refresh failure."""
        cached = self._options_oi.get_last_report(underlying)
        try:
            report = await self._options_oi.analyze(underlying, self._client, expiry_filter=expiry)
            if not options_report_has_market_data(report):
                warning = f"{underlying.upper()} options OI refresh returned no usable market data."
                logger.warning("options_oi_refresh_unusable", underlying=underlying, cached_report=bool(cached))
                if cached:
                    return {
                        **cached.model_dump(),
                        "cache_state": "stale",
                        "refresh_warning": warning,
                    }
                return {"error": warning + " No previous report is cached."}
            return {**report.model_dump(), "cache_state": "updated"}
        except Exception as e:
            logger.error("options_oi_analysis_error", underlying=underlying, error=str(e))
            if cached:
                return {
                    **cached.model_dump(),
                    "cache_state": "stale",
                    "refresh_warning": f"Refresh failed: {e}",
                }
            return {"error": str(e)}

    def get_options_oi_cached(self, underlying: str = "NIFTY") -> dict[str, Any]:
        """Return last computed options OI report."""
        report = self._options_oi.get_last_report(underlying)
        return {**report.model_dump(), "cache_state": "cached"} if report else {"error": "No data yet. Run scan first."}

    async def get_options_oi_comparison(self, underlying: str = "NIFTY") -> dict[str, Any]:
        """Compare current vs next expiry options OI."""
        try:
            comp = await self._options_oi.analyze_comparison(underlying, self._client)
            return comp.model_dump()
        except Exception as e:
            logger.error("options_oi_comparison_error", underlying=underlying, error=str(e))
            return {"error": str(e)}

    def get_pcr_history(self, underlying: str = "NIFTY") -> list[dict[str, Any]]:
        return self._options_oi.get_pcr_history(underlying)

    def get_straddle_history(self, underlying: str = "NIFTY") -> list[dict[str, Any]]:
        return self._options_oi.get_straddle_history(underlying)

    async def start_oi_tracking(
        self,
        nifty_spot: float = 0.0,
        sensex_spot: float = 0.0,
        instruments: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Start OI tracking + its 2s broadcast loop over /ws/oi. Self-sufficient:
        fetches NFO instruments and index spot prices itself if not supplied, so
        it can be called with an empty body as soon as the OI Tracker tab opens.
        """
        if instruments:
            self._oi_tracker.register_instruments(instruments)
        elif not self._oi_tracker.get_tracked_tokens():
            try:
                nfo_instruments = await self._client.get_instruments("NFO")
                self._oi_tracker.register_instruments([i.model_dump() for i in nfo_instruments])
            except Exception as e:
                logger.error("oi_tracking_instrument_fetch_failed", error=str(e))

        if nifty_spot <= 0:
            nifty_spot = await self._options_oi._get_spot_price(self._client, "NIFTY")
        if sensex_spot <= 0:
            sensex_spot = await self._options_oi._get_spot_price(self._client, "SENSEX")

        if nifty_spot > 0:
            self._oi_tracker.set_spot_price("NIFTY", nifty_spot)
        if sensex_spot > 0:
            self._oi_tracker.set_spot_price("SENSEX", sensex_spot)

        tokens = self._oi_tracker.get_tracked_tokens()
        if not tokens:
            nifty_tokens = self._oi_tracker.get_nifty_option_tokens(nifty_spot) if nifty_spot else []
            sensex_tokens = self._oi_tracker.get_sensex_option_tokens(sensex_spot) if sensex_spot else []
            tokens = nifty_tokens + sensex_tokens

        if tokens:
            # subscribe() always records tokens for auto-resubscribe even if the
            # ticker isn't connected yet (e.g. live trading not started).
            await self._ticker.subscribe(tokens, TickMode.FULL)

        if not self._oi_streaming:
            self._oi_streaming = True
            task = asyncio.create_task(self._oi_broadcast_loop())
            task.add_done_callback(self._task_exception_handler)
            # Real SL/target monitoring for OI positions is handled by
            # ExecutionEngine.start_reconciliation_loop() (protective broker
            # orders + fill detection) - see oi_strategy_execute_signal().

        logger.info("oi_tracking_started", token_count=len(tokens), ticker_connected=self._ticker.is_connected)
        return {
            "success": True,
            "tracked_tokens": len(tokens),
            "ticker_connected": self._ticker.is_connected,
        }

    async def _oi_broadcast_loop(self) -> None:
        while self._oi_streaming:
            try:
                data = self._oi_tracker.get_all_tracked_data()
                await self._oi_tracker.broadcast_oi_update(data)
            except Exception as e:
                logger.error("oi_broadcast_error", error=str(e))
            await asyncio.sleep(2)

    # ── OI Strategy Methods ───────────────────────────────────

    async def oi_strategy_scan(self, underlying: str = "NIFTY") -> dict[str, Any]:
        """Run OI strategy scan: fetch options OI → detect trading signals."""
        try:
            report = await self._options_oi.analyze(underlying, self._client)
            result = self._oi_strategy.scan_for_signals(report)
            # Auto-execute if configured
            if self._oi_strategy.config.auto_execute and result.signals:
                for sig in result.signals:
                    if sig.confidence >= 65:
                        pos = self._oi_strategy.create_position_from_signal(sig)
                        # Record in journal
                        self._record_oi_journal_entry(pos, "ENTRY")
            return result.model_dump()
        except Exception as e:
            logger.error("oi_strategy_scan_error", underlying=underlying, error=str(e))
            return {"error": str(e)}

    async def oi_strategy_scan_both(self) -> dict[str, Any]:
        """Scan both NIFTY and SENSEX for OI signals."""
        nifty = await self.oi_strategy_scan("NIFTY")
        sensex = await self.oi_strategy_scan("SENSEX")
        return {"nifty": nifty, "sensex": sensex}

    async def _oi_bridge_market_data_fetcher(self, underlying: str, days: int):
        """market_data_fetcher callable for OIFnOBridge.backtest_strategy():
        returns (DataFrame, instrument_token) for the underlying's spot/index history.

        Uses the interval configured on the bridge thresholds (intraday by
        default) so weekly-expiry option structures get intraday bars and the
        regime filter has enough bars to engage. Falls back to daily if the
        intraday cache/broker returns nothing."""
        import pandas as pd
        token = self.resolve_underlying_token(underlying)
        if not token:
            return None, 0
        interval = getattr(self._oi_fno_bridge.thresholds, "backtest_interval", "60minute")
        rows = await self._resolve_ohlcv_rows(token, interval, days=days)
        if not rows and interval != "day":
            logger.warning(
                "oi_bridge_intraday_unavailable_falling_back_daily",
                underlying=underlying, interval=interval,
            )
            rows = await self._resolve_ohlcv_rows(token, "day", days=days)
        if not rows:
            return None, token
        df = pd.DataFrame(rows).rename(columns={"ts": "timestamp"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed")
        df.set_index("timestamp", inplace=True)
        return df, token

    async def _resolve_option_tradingsymbol(
        self, underlying: str, expiry: str, strike: float, option_type: str
    ) -> Optional[tuple[str, str]]:
        """Look up the authoritative broker tradingsymbol for an option leg from the
        live NFO/BFO instrument master - never trust a locally-constructed symbol
        string for real order placement (weekly-expiry naming rules are easy to get
        wrong and would risk sending an order for the wrong contract).
        """
        exchange = "BFO" if underlying.upper() == "SENSEX" else "NFO"
        try:
            instruments = await self._client.get_instruments(exchange)
        except Exception as e:
            logger.error("oi_option_resolve_fetch_failed", exchange=exchange, error=str(e))
            return None

        expiry_key = (expiry or "")[:10]
        for inst in instruments:
            if inst.instrument_type != option_type:
                continue
            if inst.name.upper() != underlying.upper():
                continue
            if str(inst.expiry or "")[:10] != expiry_key:
                continue
            if abs(float(inst.strike) - float(strike)) > 0.01:
                continue
            return inst.tradingsymbol, exchange
        return None

    async def oi_strategy_execute_signal(self, signal_id: str) -> dict[str, Any]:
        """Validate an OI signal via a gated backtest of its mapped F&O strategy,
        then place a REAL broker order for the signal's option leg.
        """
        sig = next((s for s in self._oi_strategy._signals_history if s.id == signal_id), None)
        if not sig:
            logger.warning(
                "oi_signal_execute_not_found",
                signal_id=signal_id,
                history_size=len(self._oi_strategy._signals_history),
            )
            return {"error": f"Signal {signal_id} not found. Re-run the scan and try again."}

        open_count = len(self._oi_strategy.get_open_positions())
        max_allowed = self._oi_strategy.config.max_open_positions
        if open_count >= max_allowed:
            logger.warning(
                "oi_signal_execute_blocked",
                signal_id=signal_id,
                reason="max_open_positions",
                open_count=open_count,
                max_allowed=max_allowed,
            )
            return {"error": f"Max open positions reached ({open_count}/{max_allowed}). Close a position first."}

        # Real SL/target orders are placed by ExecutionEngine's reconciliation loop
        # right after the entry fills - refuse to trade if nothing will ever pick
        # that fill up (would leave a real position with no protective orders).
        if not self._execution._running:
            logger.warning("oi_signal_execute_no_reconciliation", signal_id=signal_id)
            return {"error": "Live trading isn't running, so the entry fill wouldn't be monitored and no protective stop-loss/target order would be placed. Start live trading first."}

        # ── Step 1: hard backtest gate - map the signal to its FnO strategy family
        # and refuse to execute unless that strategy's backtest clears quality thresholds.
        plan = await self._oi_fno_bridge.create_execution_plan(
            sig, market_data_fetcher=self._oi_bridge_market_data_fetcher
        )
        if not plan.execution_approved:
            logger.warning(
                "oi_signal_execute_rejected_by_backtest",
                signal_id=signal_id,
                fno_strategy=plan.mapping.get("fno_strategy"),
                reasons=plan.rejection_reasons,
            )
            return {
                "error": "Backtest validation failed - execution blocked.",
                "rejection_reasons": plan.rejection_reasons,
                "mapping": plan.mapping,
                "backtest": plan.backtest,
            }

        # ── Step 2: resolve the real broker tradingsymbol for this option leg.
        resolved = await self._resolve_option_tradingsymbol(sig.underlying, sig.expiry, sig.strike, sig.option_type)
        if not resolved:
            logger.error(
                "oi_signal_execute_resolve_failed",
                signal_id=signal_id, underlying=sig.underlying, strike=sig.strike,
                option_type=sig.option_type, expiry=sig.expiry,
            )
            return {"error": f"Could not resolve a live instrument for {sig.underlying} {sig.strike}{sig.option_type} exp {sig.expiry}."}
        tradingsymbol, exchange_str = resolved

        # ── Step 3: create the (paper-tracked, dashboard-facing) OIPosition FIRST so
        # its id can be carried in the entry Signal's metadata - this is how the
        # ExecutionEngine exit callback finds it again when SL/target fires later.
        pos = self._oi_strategy.create_position_from_signal(sig)
        pos.tradingsymbol = tradingsymbol
        pos.metadata["fno_strategy"] = plan.mapping.get("fno_strategy")

        # ── Step 4: place the real order - reuses ExecutionEngine's full safety
        # stack (kill switch, RiskManager limits, market-state guard, freeze-split).
        # stop_loss/target on Signal are POINTS DISTANCE from entry (matching how
        # ExecutionEngine interprets them for its own protective SL/target orders -
        # NOT absolute prices, which is what OISignal.stop_loss/target actually are).
        order_signal = Signal(
            tradingsymbol=tradingsymbol,
            exchange=Exchange(exchange_str),
            transaction_type=TransactionType.BUY if sig.action.startswith("BUY") else TransactionType.SELL,
            quantity=sig.lot_size * sig.lots,
            order_type=OrderType.MARKET,
            product=ProductType.NRML,
            stop_loss=abs(sig.entry_price - sig.stop_loss) if sig.stop_loss else None,
            target=abs(sig.target - sig.entry_price) if sig.target else None,
            strategy_name=f"oi_strategy_{sig.signal_type}",
            confidence=sig.confidence,
            metadata={"signal_id": sig.id, "position_id": pos.id, "oi_underlying": sig.underlying, "fno_strategy": plan.mapping.get("fno_strategy")},
        )

        try:
            order_id = await self._execution.execute_signal(order_signal)
        except KillSwitchError as e:
            logger.error("oi_signal_execute_kill_switch", signal_id=signal_id, error=str(e))
            return {"error": "Kill switch is active - cannot place orders."}
        except RiskLimitError as e:
            logger.warning("oi_signal_execute_risk_blocked", signal_id=signal_id, error=str(e))
            return {"error": f"Blocked by risk manager: {e}"}
        except Exception as e:
            logger.error("oi_signal_execute_order_failed", signal_id=signal_id, error=str(e))
            return {"error": f"Order placement failed: {e}"}

        if not order_id:
            logger.error("oi_signal_execute_no_order_id", signal_id=signal_id)
            return {"error": "Order placement failed - no order ID returned by broker."}

        # Entry order_id is how manual close finds the ExecutionEngine TrackedPosition later.
        pos.metadata["order_id"] = order_id
        self._record_oi_journal_entry(pos, "ENTRY")

        # Surface it the same way regular strategy signals are - in the Signals
        # feed/log and over the live websocket - so it's visible outside this tab.
        signal_data = {
            "timestamp": pos.entry_time,
            "strategy": f"oi_strategy_{pos.signal_type}",
            "tradingsymbol": pos.tradingsymbol,
            "transaction_type": pos.direction,
            "quantity": pos.quantity,
            "price": pos.entry_price,
            "confidence": sig.confidence,
            "metadata": {"position_id": pos.id, "signal_id": sig.id, "underlying": pos.underlying, "order_id": order_id},
        }
        self._signal_log.append(signal_data)
        await self._broadcast_ws({"type": "signal", "data": signal_data})

        logger.info(
            "oi_signal_executed",
            signal_id=signal_id,
            position_id=pos.id,
            order_id=order_id,
            tradingsymbol=pos.tradingsymbol,
            entry_price=pos.entry_price,
        )
        return {
            "success": True,
            "position": pos.model_dump(),
            "order_id": order_id,
            "mapping": plan.mapping,
            "backtest": plan.backtest,
        }

    async def oi_strategy_close_position(self, position_id: str, exit_price: float = 0.0) -> dict[str, Any]:
        """Manually close an OI strategy position: cancels any resting protective
        SL/target orders and places a real market exit, via ExecutionEngine's own
        close_position() (the same path used for MIS square-off) - avoids building
        a second, competing order-placement path.
        """
        pos = next((p for p in self._oi_strategy._positions if p.id == position_id and p.status == "OPEN"), None)
        if not pos:
            logger.warning("oi_position_close_not_found", position_id=position_id)
            return {"error": f"Position {position_id} not found or not open"}
        entry_order_id = pos.metadata.get("order_id")
        if not entry_order_id:
            logger.error("oi_position_close_no_order_id", position_id=position_id)
            return {"error": "Position has no tracked entry order - cannot place a real exit order"}

        exit_order_id = await self._execution.close_position(entry_order_id, reason="Manual close")
        if not exit_order_id:
            logger.error("oi_position_close_failed", position_id=position_id, entry_order_id=entry_order_id)
            return {"error": "Exit order placement failed - position remains open. Check broker/orders manually."}

        # _on_execution_exit (registered as ExecutionEngine's on_exit callback) finalizes
        # the OIPosition, journal, and signal log/broadcast once the exit is confirmed.
        return {"success": True, "order_id": exit_order_id}

    def oi_strategy_get_positions(self, underlying: Optional[str] = None) -> dict[str, Any]:
        """Get open and recent closed positions."""
        open_pos = self._oi_strategy.get_open_positions(underlying)
        closed_pos = self._oi_strategy.get_closed_positions(underlying, limit=30)
        return {
            "open": [p.model_dump() for p in open_pos],
            "closed": [p.model_dump() for p in closed_pos],
            "open_count": len(open_pos),
            "closed_count": len(closed_pos),
        }

    def oi_strategy_get_signals(self, underlying: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent signal history."""
        signals = self._oi_strategy.get_signal_history(underlying, limit)
        return [s.model_dump() for s in signals]

    def oi_strategy_get_summary(self) -> dict[str, Any]:
        """Get overall OI strategy performance summary."""
        return self._oi_strategy.get_strategy_summary()

    def oi_strategy_update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Update OI strategy configuration."""
        cfg = self._oi_strategy.update_config(updates)
        return cfg.model_dump()

    def oi_strategy_get_config(self) -> dict[str, Any]:
        """Get current OI strategy configuration."""
        return self._oi_strategy.config.model_dump()

    def _record_oi_journal_entry(self, position: "OIPosition", action: str) -> None:
        """Record OI strategy entry/exit in the journal system."""
        try:
            meta = self._oi_strategy.get_journal_metadata(position)
            pnl = position.pnl if action == "EXIT" else 0.0
            txn = "BUY" if position.direction == "BUY" else "SELL"
            if action == "EXIT":
                txn = "SELL" if position.direction == "BUY" else "BUY"
            self._journal.record_trade(
                strategy=f"oi_strategy_{position.signal_type}",
                tradingsymbol=position.tradingsymbol or f"{position.underlying}_{position.option_type}_{position.strike}",
                exchange=position.exchange,
                transaction_type=txn,
                quantity=position.quantity,
                price=position.entry_price if action == "ENTRY" else position.exit_price,
                order_id=position.id,
                status="COMPLETE",
                pnl=pnl,
                notes=f"OI Strategy: {position.signal_type} | {action}",
                metadata=meta,
            )
            # Also record in JournalStore (pro journal / strategy order book) - on
            # ENTRY as an open position, upserted to closed on EXIT (same entry_id).
            entry = JournalEntry(
                entry_id=f"oi-{position.id}",
                trade_type="fno",
                instrument=position.tradingsymbol or f"{position.underlying}_{position.option_type}_{position.strike}",
                tradingsymbol=position.tradingsymbol or "",
                exchange=position.exchange,
                strategy_name=f"oi_strategy_{position.signal_type}",
                direction=position.direction,
                is_closed=(action == "EXIT"),
                gross_pnl=position.pnl,
                net_pnl=position.pnl,
                entry_time=position.entry_time,
                exit_time=position.exit_time or "",
                tags=["oi_strategy", position.signal_type, position.underlying],
                notes=f"OI Signal: {position.signal_type}",
                metadata=meta,
            )
            self._journal_store.record_entry(entry)
        except Exception as e:
            logger.error("oi_journal_record_error", error=str(e))

    def add_strategy(self, name: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        strategy_map = {
            "ema_crossover": EMACrossoverStrategy,
            "vwap_breakout": VWAPBreakoutStrategy,
            "mean_reversion": MeanReversionStrategy,
            "rsi": RSIStrategy,
            "scanner_strategy": ScannerStrategy,
            "iron_condor": IronCondorStrategy,
            "straddle_strangle": StraddleStrangleStrategy,
            "bull_call_spread": BullCallSpreadStrategy,
            "bear_put_spread": BearPutSpreadStrategy,
        }
        # Merge the analysis strategies so they can be registered for live
        # trading too (the Add-Strategy modal offers macd_crossover etc., which
        # previously fell through to "Unknown strategy" because they were only
        # merged in _resolve_strategy, not here).
        from src.strategy.analysis_strategies import ANALYSIS_STRATEGIES
        strategy_map.update(ANALYSIS_STRATEGIES)
        strategy_map["day_trade_setups"] = DayTradeSetupsStrategy
        cls = strategy_map.get(name)
        if cls:
            strategy = cls(params)
        else:
            # Check custom strategies
            custom = self._custom_store.build_strategy(name)
            if custom:
                strategy = custom
            else:
                return {"error": f"Unknown strategy: {name}", "available": list(strategy_map.keys())}
        # Avoid duplicate registration of the same strategy name.
        if any(s.name == strategy.name for s in self._strategies):
            return {"error": f"Strategy '{strategy.name}' is already added"}
        # run live on the strategy's own timeframe; added while live, it starts
        # from the bars already built (or seeds that timeframe)
        interval = self.live_interval(strategy)
        strategy.set_timeframe(interval)
        for (iv, token), df in self._live_bars.items():
            if iv == interval:
                strategy.update_bar_data(token, df)
        self._strategies.append(strategy)
        if self._running and self._subscribed_tokens and not any(iv == interval for iv, _ in self._live_bars):
            try:
                task = asyncio.get_running_loop().create_task(
                    self._seed_bar_data(list(self._subscribed_tokens), interval=Interval(interval)))
                task.add_done_callback(self._task_exception_handler)
            except RuntimeError:
                pass
        logger.info("strategy_added", name=name)
        return {"success": True, "name": strategy.name}

    async def promote_backtest_to_live(
        self, strategy_name: str, params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Promote the last backtested strategy to a live (tick-driven) strategy,
        then subscribe to its symbol and seed bar data so it actually fires.

        Only allowed when the cached backtest's health report marks it
        execution-ready. This is the bridge between "backtest passed" and
        "showing in the Strategy tab and generating signals" — without it an
        execution-ready backtest never ran live, and a registered strategy with
        no subscribed/seeded symbol never produced a signal.
        """
        result = self._backtest_results
        if not result or "error" in result:
            return {"error": "Run a backtest first."}
        if result.get("strategy_name") and result["strategy_name"] != strategy_name:
            logger.warning(
                "promote_strategy_mismatch",
                requested=strategy_name, backtested=result.get("strategy_name"),
            )
        health = result.get("health_report") or {}
        if not health.get("execution_ready"):
            return {
                "error": "Strategy is not execution-ready — resolve the health report blockers first.",
                "blockers": health.get("blockers", []),
            }

        # Resolve the instrument the backtest ran on so the live strategy can be
        # fed ticks for it. Prefer the token captured at backtest time.
        symbol = result.get("tradingsymbol", "")
        exchange = result.get("exchange", "NSE")
        token = result.get("instrument_token", 0)
        if not token and symbol:
            token = await self.resolve_instrument_token(symbol, exchange) or 0
        if not token:
            return {"error": f"Could not resolve an instrument token for {exchange}:{symbol}."}

        # Carry the symbol->token mapping into the strategy's params so its
        # generated signals carry the real tradingsymbol instead of TOKEN_<id>.
        params = dict(params or {})
        tmap = dict(params.get("tradingsymbol_map", {}))
        tmap[token] = symbol
        # Some strategies key the map by str(token); cover both.
        tmap[str(token)] = symbol
        params["tradingsymbol_map"] = tmap
        # run live on the timeframe it was backtested on
        params.setdefault("live_interval", result.get("interval", "5minute"))

        added = self.add_strategy(strategy_name, params)
        if "error" in added:
            return added

        # Subscribe + seed bar data so the strategy actually receives ticks and
        # has enough history to emit signals (200-DMA filter needs history).
        subscribed = False
        seeded = False
        try:
            await self._ticker.subscribe([token], TickMode.QUOTE)
            if token not in self._subscribed_tokens:
                self._subscribed_tokens.append(token)
            subscribed = True
        except Exception as e:
            logger.warning("promote_subscribe_failed", token=token, error=str(e))
        try:
            # Seed the SAME interval the strategy was backtested on so live
            # signals are computed on the validated timeframe (not always 5m).
            seed_interval = Interval(result.get("interval", "5minute"))
            await self._seed_bar_data([token], interval=seed_interval)
            seeded = True
        except Exception as e:
            logger.warning("promote_seed_failed", token=token, error=str(e))

        logger.info(
            "strategy_promoted_to_live",
            name=strategy_name, symbol=symbol, token=token,
            subscribed=subscribed, seeded=seeded, live=self._running,
        )
        note = "Added to live strategies."
        if self._running:
            note += " Subscribed + bar data seeded — it will generate signals on live ticks now."
        else:
            note += " Live trading isn't running — start it to begin receiving signals for the subscribed symbol."
        return {
            "success": True,
            "name": strategy_name,
            "symbol": symbol,
            "instrument_token": token,
            "subscribed": subscribed,
            "bar_data_seeded": seeded,
            "live_trading_running": self._running,
            "note": note,
        }

    def remove_strategy(self, name: str) -> dict[str, Any]:
        self._strategies = [s for s in self._strategies if s.name != name]
        return {"success": True, "removed": name}

    def toggle_strategy(self, name: str) -> dict[str, Any]:
        for s in self._strategies:
            if s.name == name:
                if s.is_active:
                    s.deactivate()
                else:
                    s.activate()
                return {"name": name, "is_active": s.is_active}
        return {"error": f"Strategy {name} not found"}

    async def start_live(self, tokens: list[int], mode: str = "quote") -> dict[str, Any]:
        if self._running:
            return {"error": "Already running"}

        self._running = True
        self._subscribed_tokens = tokens

        self._ticker.on_ticks = self._on_ticks
        self._ticker.on_connect = self._on_connect
        self._ticker.on_close = self._on_close
        self._ticker.on_error = self._on_error
        self._ticker.on_order_update = self._on_order_update

        await self._seed_bar_data(tokens)
        await self._ticker.subscribe(tokens, TickMode(mode))

        if self._board.settings.get("enabled"):
            self._ensure_day_trade_loop()   # board auto-trade runs alongside live trading
        for coro in [
            self._ticker.connect(),
            self._bar_flush_loop(),
            self._execution.start_reconciliation_loop(),
            self._position_update_loop(),
            self._oi_snapshot_loop(),
        ]:
            task = asyncio.create_task(coro)
            task.add_done_callback(self._task_exception_handler)

        # Kick off OI tracking too - the ticker connecting is what makes OI data
        # genuinely live (ticks -> _oi_tracker.update_from_tick); harmless if
        # already running or if /ws/oi hasn't been opened yet.
        try:
            await self.start_oi_tracking()
        except Exception as e:
            logger.warning("oi_tracking_autostart_failed", error=str(e))

        logger.info("live_trading_started", tokens=tokens)
        return {"success": True, "tokens": tokens}

    async def stop_live(self) -> dict[str, Any]:
        self._running = False
        self._bar_aggs = {}
        self._live_bars = {}
        await self._execution.stop_reconciliation_loop()
        await self._ticker.disconnect()
        logger.info("live_trading_stopped")
        return {"success": True}

    def register_ws_client(self, ws: Any) -> None:
        self._ws_clients.append(ws)

    def unregister_ws_client(self, ws: Any) -> None:
        if ws in self._ws_clients:
            self._ws_clients.remove(ws)

    async def _broadcast_ws(self, data: dict[str, Any]) -> None:
        dead: list[Any] = []
        for ws in self._ws_clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.remove(ws)

    # Bar size (minutes) per live interval; "day" is one bar per 375-minute session.
    LIVE_INTERVAL_MINUTES: dict[str, int] = {
        "minute": 1, "3minute": 3, "5minute": 5, "10minute": 10,
        "15minute": 15, "30minute": 30, "60minute": 60, "day": 375,
    }
    # Default history to seed per interval (strategies can ask for more via history_days).
    LIVE_SEED_DAYS: dict[str, int] = {"15minute": 60, "30minute": 60, "60minute": 100, "day": 400}
    MAX_LIVE_BARS = 3000

    @classmethod
    def live_interval(cls, strategy: Any) -> str:
        """Timeframe a strategy runs on live: its required bar size, else the
        interval it was backtested on (``live_interval`` param), else 5-minute."""
        interval = getattr(strategy, "required_interval", None) or (getattr(strategy, "params", None) or {}).get("live_interval")
        return interval if interval in cls.LIVE_INTERVAL_MINUTES else "5minute"

    async def _seed_bar_data(self, tokens: list[int], interval: Interval | None = None, days: int | None = None) -> None:
        """Load recent history for each live timeframe in use and hand it to the
        strategies on that timeframe. The still-forming bar primes the
        aggregator; strategies only ever receive completed bars."""
        intervals = [interval.value] if interval else sorted({self.live_interval(s) for s in self._strategies} or {"5minute"})
        for iv in intervals:
            on_iv = [s for s in self._strategies if self.live_interval(s) == iv]
            seed_days = days or max(
                [self.LIVE_SEED_DAYS.get(iv, 5)] + [int(getattr(s, "history_days", 0) or 0) for s in on_iv]
            )
            agg = self._bar_aggs.setdefault(iv, BarAggregator(self.LIVE_INTERVAL_MINUTES[iv]))
            to_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            from_date = (datetime.now() - timedelta(days=seed_days)).strftime("%Y-%m-%d %H:%M:%S")
            for token in tokens:
                try:
                    candles = await self._client.get_historical_data(
                        instrument_token=token,
                        interval=Interval(iv),
                        from_date=from_date,
                        to_date=to_date,
                    )
                    if not candles:
                        continue
                    rows = [
                        {"ts": c.timestamp, "open": c.open, "high": c.high,
                         "low": c.low, "close": c.close, "volume": c.volume}
                        for c in candles
                    ]
                    try:
                        self._market_store.store_candles(token, iv, rows)
                    except Exception as e:
                        logger.warning("bar_data_persist_failed", token=token, error=str(e))
                    df = pd.DataFrame([
                        {"timestamp": str(r["ts"]), "open": r["open"], "high": r["high"],
                         "low": r["low"], "close": r["close"], "volume": r["volume"]}
                        for r in rows
                    ])
                    current = bucket_start(datetime.now(), agg.minutes)
                    if not df.empty and datetime.fromisoformat(str(df["timestamp"].iloc[-1])[:19]) >= current:
                        agg.prime(token, df.iloc[-1].to_dict())
                        df = df.iloc[:-1].reset_index(drop=True)
                    self._live_bars[(iv, token)] = df
                    for strategy in on_iv:
                        strategy.set_timeframe(iv)
                        strategy.update_bar_data(token, df)
                except Exception as e:
                    logger.warning("bar_data_seed_failed", token=token, interval=iv, error=str(e))

    @staticmethod
    def resolve_underlying_token(underlying: str, instrument_token: int = 0) -> int:
        """Resolve an F&O underlying index token: client-supplied token wins, else
        looked up from the well-known index token map (NIFTY/BANKNIFTY/etc.)."""
        if instrument_token:
            return instrument_token
        return INDEX_TOKENS.get(underlying.upper(), 0)

    async def _resolve_ohlcv_rows(
        self,
        instrument_token: int,
        interval: str,
        days: int = 365,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Historical OHLCV rows for backtest/paper-trade: locally cached data first
        (populated by ``import_historical_data`` or a prior run), live Kite fetch as
        fallback - which is itself cached for next time.
        """
        from datetime import timedelta
        to_dt = datetime.now()
        to_date_s = (to_date or to_dt.strftime("%Y-%m-%d"))[:10]
        from_date_s = (from_date or (to_dt - timedelta(days=days)).strftime("%Y-%m-%d"))[:10]

        cached = self._market_store.get_candles(instrument_token, interval, from_date_s, to_date_s)
        if cached:
            return cached

        from_dt_full = from_date or (to_dt - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        to_dt_full = to_date or to_dt.strftime("%Y-%m-%d %H:%M:%S")
        candles = await self._client.get_historical_data(
            instrument_token=instrument_token,
            interval=Interval(interval),
            from_date=from_dt_full,
            to_date=to_dt_full,
        )
        rows = [
            {"ts": c.timestamp, "open": c.open, "high": c.high, "low": c.low,
             "close": c.close, "volume": c.volume}
            for c in candles
        ] if candles else []
        if rows:
            try:
                self._market_store.store_candles(instrument_token, interval, rows)
            except Exception as e:
                logger.warning("ohlcv_cache_store_failed", token=instrument_token, error=str(e))
        return rows

    async def import_historical_data(
        self,
        instrument_token: int,
        tradingsymbol: str,
        interval: str = "day",
        days: int = 365,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Import historical candles from Zerodha into the local DB cache.

        Incremental in BOTH directions:
          - if the cache already reaches up to (or past) ``to_date``, only the
            gap since the last cached bar is fetched (cheap top-up).
          - if the cache's oldest bar is newer than the requested ``from_date``
            (e.g. the user increases "Days" to pull in more history than was
            previously imported), the older gap is ALSO backfilled instead of
            being silently ignored (this was a real bug: the old logic only
            ever checked the "recent" edge, so raising "Days" had no effect
            once ANY data existed for this token+interval - every click just
            re-fetched today's single bar forever).
        """
        from datetime import timedelta
        to_dt = datetime.now()
        to_date_s = (to_date or to_dt.strftime("%Y-%m-%d"))[:10]
        requested_from_s = (from_date or (to_dt - timedelta(days=days)).strftime("%Y-%m-%d"))[:10]

        cached_min, cached_max = self._market_store.get_cached_date_range(instrument_token, interval)
        incremental = bool(cached_max)
        fetch_ranges: list[tuple[str, str]] = []

        if not cached_max:
            # Nothing cached yet - fetch the full requested range.
            fetch_ranges.append((requested_from_s, to_date_s))
        else:
            if requested_from_s < cached_min:
                # Backfill older history the cache doesn't have yet.
                fetch_ranges.append((requested_from_s, cached_min))
            if cached_max <= to_date_s:
                # Top up from the last cached bar (re-fetches that day too,
                # harmlessly upserted, to complete any partial day).
                fetch_ranges.append((cached_max, to_date_s))

        stored = 0
        try:
            for range_from, range_to in fetch_ranges:
                candles = await self._client.get_historical_data(
                    instrument_token=instrument_token,
                    interval=Interval(interval),
                    from_date=f"{range_from} 00:00:00",
                    to_date=f"{range_to} 23:59:59",
                )
                rows = [
                    {"ts": c.timestamp, "open": c.open, "high": c.high, "low": c.low,
                     "close": c.close, "volume": c.volume}
                    for c in candles
                ] if candles else []
                if rows:
                    stored += self._market_store.store_candles(instrument_token, interval, rows)
        except Exception as e:
            logger.error("historical_import_fetch_failed", token=instrument_token, error=str(e))
            return {"error": f"Failed to fetch data: {e}"}

        new_min, new_max = self._market_store.get_cached_date_range(instrument_token, interval)

        logger.info(
            "historical_data_imported",
            tradingsymbol=tradingsymbol,
            instrument_token=instrument_token,
            interval=interval,
            incremental=incremental,
            candles_fetched=stored,
            fetch_ranges=fetch_ranges,
        )
        return {
            "success": True,
            "tradingsymbol": tradingsymbol,
            "instrument_token": instrument_token,
            "interval": interval,
            "incremental": incremental,
            "candles_fetched": stored,
            "fetched_from": fetch_ranges[0][0] if fetch_ranges else requested_from_s,
            "fetched_to": to_date_s,
            "cached_range": {"from": new_min, "to": new_max},
        }

    # Timeframes checked by test_stock_strategy(), each with its own lookback window.
    STOCK_TEST_TIMEFRAMES: list[tuple[str, int]] = [
        ("5minute", 100),
        ("15minute", 200),
        ("60minute", 400),
        ("day", 2000),
    ]

    OOS_FRACTION = 0.3       # last 30% of a stock test's bars is the out-of-sample segment
    OOS_MIN_TRADES = 3
    TEST_RISK_PER_TRADE = 0.01   # stock tests size each trade to risk 1% of capital

    # Single-stock equity strategies offered for testing / live use, with display labels.
    EQUITY_STRATEGY_LABELS: dict[str, str] = {
        "ema_crossover": "EMA Crossover",
        "rsi": "RSI Strategy",
        "vwap_breakout": "VWAP Breakout",
        "mean_reversion": "Mean Reversion",
        "scanner_strategy": "Scanner Strategy (All-in-One)",
        "day_trade_setups": "Day-Trade Setups (5-min: ORB / PDH-PDL / VWAP / pullback / squeeze)",
    }

    @staticmethod
    def _equity_strategy_classes() -> dict[str, type]:
        from src.strategy.analysis_strategies import ANALYSIS_STRATEGIES

        classes: dict[str, type] = {
            "ema_crossover": EMACrossoverStrategy,
            "vwap_breakout": VWAPBreakoutStrategy,
            "mean_reversion": MeanReversionStrategy,
            "rsi": RSIStrategy,
            "rsi_strategy": RSIStrategy,
            "scanner_strategy": ScannerStrategy,
            "day_trade_setups": DayTradeSetupsStrategy,
        }
        classes.update(ANALYSIS_STRATEGIES)
        return classes

    def stock_test_timeframes(self, strategy: Any) -> list[tuple[str, int]]:
        """Timeframes a single-stock test runs on: the strategy's required bar
        size if it has one (e.g. 5-minute day-trade setups), else all four."""
        required = getattr(strategy, "required_interval", None)
        if required:
            days = dict(self.STOCK_TEST_TIMEFRAMES).get(required, 100)
            return [(required, days)]
        return list(self.STOCK_TEST_TIMEFRAMES)

    def get_strategy_catalog(self) -> list[dict[str, Any]]:
        """Every single-stock strategy that can be tested or traded, grouped for pickers."""
        from src.strategy.analysis_strategies import ANALYSIS_STRATEGIES, ANALYSIS_STRATEGY_LABELS

        classes = self._equity_strategy_classes()

        def entry(name: str, label: str, group: str, cls: Any = None) -> dict[str, Any]:
            required = getattr(cls, "required_interval", None) if cls else None
            frames = [required] if required else [tf for tf, _ in self.STOCK_TEST_TIMEFRAMES]
            return {"name": name, "label": label, "group": group, "timeframes": frames}

        catalog = [
            entry(name, label, "Equity strategies", classes[name])
            for name, label in self.EQUITY_STRATEGY_LABELS.items()
        ]
        catalog += [
            entry(name, ANALYSIS_STRATEGY_LABELS.get(name, name.replace("_", " ").title()), "Analysis strategies", cls)
            for name, cls in ANALYSIS_STRATEGIES.items()
        ]
        catalog += [
            entry(c["name"], c["name"], "Custom strategies")
            for c in self._custom_store.list_strategies()
        ]
        return catalog

    # Extra calendar days to fetch before the requested window so the 200-DMA
    # regime filter (BaseStrategy._passes_dma200_filter) has its 200 bars of
    # history by the time the user's period starts. Without this, a day/365d
    # backtest only has ~250 bars total and the filter can't engage for the
    # first ~200 bars (entries would be blocked for 80% of the run).
    _WARMUP_CAL_DAYS: dict[str, int] = {
        "day": 320, "60minute": 55, "30minute": 30, "15minute": 16,
        "10minute": 12, "5minute": 7, "3minute": 5, "minute": 3,
    }

    @classmethod
    def _with_dma200_warmup(cls, strategy: Any, interval: str, days: int, from_date: str | None) -> int:
        """Extend the fetch window with warm-up history when the strategy's
        200-DMA filter is enabled and no explicit start date was given."""
        if from_date:
            return days  # user pinned an explicit start - respect it
        if not getattr(strategy, "params", {}).get("use_dma200_filter", True):
            return days
        return days + cls._WARMUP_CAL_DAYS.get(interval, 320)

    @staticmethod
    def _evaluate_equity_backtest(raw: dict[str, Any], oos_from: str | None = None) -> dict[str, Any]:
        """Pass/fail a single-timeframe equity backtest against fixed quality
        thresholds (mirrors OIFnOBridge's gate, relaxed min_trades since a
        single timeframe/instrument backtest won't generate 30+ trades).

        With ``oos_from`` (a timestamp), trades entered from then on form an
        out-of-sample segment that must also be profitable with at least
        ``OOS_MIN_TRADES`` trades - a strategy that only worked early in the
        window does not pass."""
        checks = {
            "win_rate": (raw.get("win_rate", 0.0), 45.0, ">="),
            "sharpe_ratio": (raw.get("sharpe_ratio", 0.0), 1.0, ">="),
            "max_drawdown_pct": (abs(raw.get("max_drawdown_pct", 0.0)), 20.0, "<="),
            "total_return_pct": (raw.get("total_return_pct", 0.0), 0.0, ">="),
            "total_trades": (float(raw.get("total_trades", 0)), 5.0, ">="),
        }
        reasons = []
        for name, (actual, threshold, op) in checks.items():
            ok = actual >= threshold if op == ">=" else actual <= threshold
            if not ok:
                reasons.append(f"{name}: {actual:.2f} {op} {threshold:.2f} \u2717")
        oos = None
        if oos_from is not None:
            closed = [t for t in raw.get("trades", []) if "exit_bar" in t and str(t.get("entry_time", "")) >= oos_from]
            oos = {
                "from": oos_from,
                "trades": len(closed),
                "pnl": round(sum(t.get("pnl", 0) for t in closed), 2),
                "win_rate": round(sum(1 for t in closed if t.get("pnl", 0) > 0) / len(closed) * 100, 1) if closed else 0.0,
            }
            if oos["trades"] < TradingService.OOS_MIN_TRADES:
                reasons.append(f"out-of-sample trades: {oos['trades']} >= {TradingService.OOS_MIN_TRADES} \u2717")
            elif oos["pnl"] <= 0:
                reasons.append(f"out-of-sample P&L: {oos['pnl']:.2f} > 0 \u2717")
        return {
            "passed": len(reasons) == 0,
            "out_of_sample": oos,
            "win_rate": raw.get("win_rate", 0.0),
            "sharpe_ratio": raw.get("sharpe_ratio", 0.0),
            "max_drawdown_pct": raw.get("max_drawdown_pct", 0.0),
            "total_return_pct": raw.get("total_return_pct", 0.0),
            "total_trades": raw.get("total_trades", 0),
            "failure_reasons": reasons,
        }

    async def test_stock_strategy(
        self,
        symbol: str,
        strategy_name: str,
        exchange: str = "NSE",
        strategy_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Download historical data for a stock across 4 timeframes (5min/100d,
        15min/200d, hourly/400d, daily/2000d) and backtest ``strategy_name`` on
        each - used by the Analysis tab's per-stock "Test Strategy" action and
        the stock page. Trades are sized to risk 1% of capital. A timeframe
        passes when it clears the quality thresholds AND its last 30% (out of
        sample) is profitable with at least 3 trades; overall pass = any
        timeframe passing.
        A strategy built for one bar size (``required_interval``, e.g. the
        5-minute day-trade setups) is tested on that timeframe only.
        """
        strategy = self._resolve_strategy(strategy_name, strategy_params)
        if isinstance(strategy, dict):
            return strategy  # error dict (unknown strategy)

        token = await self.resolve_instrument_token(symbol, exchange)
        if not token:
            return {"error": f"Instrument not found: {exchange}:{symbol}"}

        timeframes = self.stock_test_timeframes(strategy)
        timeframe_results: dict[str, Any] = {}
        for interval, days in timeframes:
            fetch_days = self._with_dma200_warmup(strategy, interval, days, None)
            try:
                await self.import_historical_data(
                    instrument_token=token, tradingsymbol=symbol, interval=interval, days=fetch_days,
                )
                rows = await self._resolve_ohlcv_rows(token, interval, days=fetch_days)
                if not rows or len(rows) < 30:
                    timeframe_results[interval] = {"passed": False, "error": "Insufficient historical data"}
                    continue
                df = pd.DataFrame(rows).rename(columns={"ts": "timestamp"})
                df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed")
                df.set_index("timestamp", inplace=True)

                # Fresh strategy instance per timeframe - avoids leaking state
                # (e.g. _prev_signal) between independent backtests.
                tf_strategy = self._resolve_strategy(strategy_name, strategy_params)
                # risk-sized positions so return / drawdown / Sharpe mean something
                engine = BacktestEngine(
                    strategy=tf_strategy, initial_capital=100_000.0,
                    position_sizing="risk_based", risk_per_trade=self.TEST_RISK_PER_TRADE,
                )
                # Backtests are CPU-bound (minutes for 5-minute bars); run them in a worker
                # thread so the event loop keeps serving requests, agents and the live feed.
                raw = await asyncio.to_thread(engine.run, df, instrument_token=token, tradingsymbol=symbol, timeframe=interval)
                if "error" in raw:
                    timeframe_results[interval] = {"passed": False, "error": raw["error"]}
                    continue
                # the requested window's last 30% must hold up on its own (the
                # warm-up history before the window is not part of the split)
                window = df.index[df.index >= df.index[-1] - pd.Timedelta(days=days)]
                split = window[int(len(window) * (1 - self.OOS_FRACTION))] if len(window) else df.index[-1]
                oos_from = str(split)   # same format the engine writes on trade entry_time
                timeframe_results[interval] = self._evaluate_equity_backtest(raw, oos_from=oos_from)
            except Exception as e:
                logger.error("stock_strategy_test_failed", symbol=symbol, interval=interval, error=str(e))
                timeframe_results[interval] = {"passed": False, "error": str(e)}

        passing = [tf for tf, r in timeframe_results.items() if r.get("passed")]
        overall_passed = len(passing) > 0
        logger.info(
            "stock_strategy_tested",
            symbol=symbol,
            strategy=strategy_name,
            overall_passed=overall_passed,
            passing_timeframes=passing,
        )
        return {
            "symbol": symbol,
            "strategy": strategy_name,
            "timeframes": timeframe_results,
            "timeframes_tested": [tf for tf, _ in timeframes],
            "overall_passed": overall_passed,
            "passing_timeframes": passing,
        }

    async def execute_stock_strategy_signal(
        self,
        symbol: str,
        strategy_name: str,
        timeframe: str,
        exchange: str = "NSE",
        quantity: int = 0,
        atr_sl_mult: float = 1.5,
        atr_target_mult: float = 3.0,
        strategy_params: dict[str, Any] | None = None,
        risk_amount: float = 1_000.0,
    ) -> dict[str, Any]:
        """Place a REAL order for a stock that passed test_stock_strategy() on
        ``timeframe`` - only if the strategy is signalling on the latest closed
        bar of freshly fetched data.

        The order carries the strategy's own stop, target and execution plan,
        exactly as tested; the ATR multiples are only a fallback for strategies
        that set no stop. Quantity defaults to ``risk_amount`` / stop distance.
        The timeframe sets the product: intraday MIS, daily CNC (long-only).
        """
        strategy = self._resolve_strategy(strategy_name, strategy_params)
        if isinstance(strategy, dict):
            return strategy
        if not self._execution._running:
            return {"error": "Live trading isn't running, so no protective stop-loss/target order would be placed. Start live trading first."}
        if timeframe not in dict(self.STOCK_TEST_TIMEFRAMES):
            return {"error": f"Unsupported timeframe: {timeframe}"}
        now = datetime.now()
        if not market_open(now):
            return {"error": "The market is closed - a signal can only be executed on the bar that has just closed."}

        token = await self.resolve_instrument_token(symbol, exchange)
        if not token:
            return {"error": f"Instrument not found: {exchange}:{symbol}"}
        tf_name = f"stock_test_{strategy_name}"
        if self._execution.open_position_for(symbol, tf_name):
            return {"error": f"A {strategy_name} position in {symbol} is already open."}

        # fresh bars from the broker (not the test's cache), completed bars only
        days = self._with_dma200_warmup(strategy, timeframe, dict(self.STOCK_TEST_TIMEFRAMES)[timeframe], None)
        try:
            candles = await self._client.get_historical_data(
                instrument_token=token, interval=Interval(timeframe),
                from_date=(now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
                to_date=now.strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception as e:
            return {"error": f"Could not fetch current {timeframe} data: {e}"}
        df = pd.DataFrame([
            {"timestamp": str(c.timestamp), "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
            for c in candles or []
        ])
        if df.empty:
            return {"error": f"No {timeframe} data returned for {symbol}."}
        starts = pd.to_datetime(df["timestamp"].str[:19])
        if timeframe == "day":
            df = df[starts.dt.date < now.date()].reset_index(drop=True)     # today's daily bar is still forming
        else:
            current = bucket_start(now, TIMEFRAME_MINUTES[timeframe])
            df = df[starts < current].reset_index(drop=True)
            last = datetime.fromisoformat(df["timestamp"].iloc[-1][:19]) if not df.empty else None
            if last is None or last < current - timedelta(minutes=TIMEFRAME_MINUTES[timeframe]):
                return {"error": f"The latest closed {timeframe} bar ({last}) is not the one that just closed - data is not current."}
        if len(df) < 30:
            return {"error": f"Insufficient {timeframe} history for {symbol}."}

        strategy.set_timeframe(timeframe)
        strategy.update_bar_data(token, df)
        signal = strategy.generate_signal(df, token)
        if not signal or not getattr(signal, "transaction_type", None):
            return {"error": f"No signal for {symbol} on the latest closed {timeframe} bar - the strategy isn't triggering now."}
        side = 1 if signal.transaction_type == TransactionType.BUY else -1
        if (signal.metadata or {}).get("action") == "exit":
            return {"error": "The strategy's current signal is an exit, not an entry."}
        if side < 0 and not allows_short(timeframe):
            return {"error": "Daily-timeframe trades are delivery (CNC) and cannot be short."}

        entry_price = float(df["close"].iloc[-1])
        stop_points, target_points = signal.stop_loss, signal.target
        if not stop_points:
            # fallback only: the strategy set no stop of its own
            atr = compute_all_indicators(df).get("atr_14") or entry_price * 0.01
            stop_points, target_points = round(atr * atr_sl_mult, 2), round(atr * atr_target_mult, 2)
        qty = quantity if quantity > 0 else max(1, int(risk_amount / stop_points))

        order_signal = signal.model_copy(update={
            "tradingsymbol": symbol,
            "exchange": Exchange(exchange),
            "quantity": qty,
            "order_type": OrderType.MARKET,
            "product": ProductType(product_for(timeframe)),
            "stop_loss": stop_points,
            "target": target_points,
            "strategy_name": tf_name,
            "metadata": {**(signal.metadata or {}), "symbol": symbol, "timeframe": timeframe,
                         "signal_bar": str(df["timestamp"].iloc[-1])},
        })

        try:
            order_id = await self._execution.execute_signal(order_signal)
        except KillSwitchError as e:
            logger.error("stock_signal_execute_kill_switch", symbol=symbol, error=str(e))
            return {"error": "Kill switch is active - cannot place orders."}
        except RiskLimitError as e:
            logger.warning("stock_signal_execute_risk_blocked", symbol=symbol, error=str(e))
            return {"error": f"Blocked by risk manager: {e}"}
        except Exception as e:
            logger.error("stock_signal_execute_order_failed", symbol=symbol, error=str(e))
            return {"error": f"Order placement failed: {e}"}

        if not order_id:
            return {"error": "Order placement failed - no order ID returned by broker."}

        self._journal.record_trade(
            strategy=tf_name,
            tradingsymbol=symbol,
            exchange=exchange,
            transaction_type=signal.transaction_type.value,
            quantity=qty,
            price=entry_price,
            order_id=order_id,
            status="ENTRY",
            notes=f"SL={stop_points} target={target_points} timeframe={timeframe} (strategy plan)",
        )
        signal_data = {
            "timestamp": now.isoformat(),
            "strategy": tf_name,
            "tradingsymbol": symbol,
            "transaction_type": signal.transaction_type.value,
            "quantity": qty,
            "price": entry_price,
            "confidence": signal.confidence,
            "metadata": {"order_id": order_id, "timeframe": timeframe, "sl_points": stop_points, "target_points": target_points},
        }
        self._signal_log.append(signal_data)
        await self._broadcast_ws({"type": "signal", "data": signal_data})
        logger.info("stock_signal_executed", symbol=symbol, strategy=strategy_name, order_id=order_id,
                    entry_price=entry_price, sl_points=stop_points, target_points=target_points, qty=qty)
        return {
            "success": True,
            "order_id": order_id,
            "symbol": symbol,
            "entry_price": entry_price,
            "quantity": qty,
            "product": product_for(timeframe),
            "stop_loss_points": stop_points,
            "target_points": target_points,
            "signal_bar": str(df["timestamp"].iloc[-1]),
        }

    async def _on_ticks(self, ticks: list[Tick]) -> None:
        self._last_tick_at = time.time()
        completed_bars: list[tuple[str, int, Any]] = []
        self._tick_count += len(ticks)
        self._emit("feed.ticks", count=len(ticks))
        quote_rows: list[dict[str, Any]] = []
        now_mono = time.monotonic()
        for tick in ticks:
            tick_data = {
                "token": tick.instrument_token,
                "ltp": tick.last_price,
                "volume": tick.volume_traded,
                "high": tick.ohlc.high,
                "low": tick.ohlc.low,
                "open": tick.ohlc.open,
                "close": tick.ohlc.close,
                "change": tick.change,
                "buy_qty": tick.total_buy_quantity,
                "sell_qty": tick.total_sell_quantity,
            }
            self._live_ticks[tick.instrument_token] = tick_data

            last_persist = self._last_quote_persist.get(tick.instrument_token, 0.0)
            if now_mono - last_persist >= self._quote_persist_interval_s:
                quote_rows.append({
                    "instrument_token": tick.instrument_token,
                    "ts": datetime.now().isoformat(),
                    "last_price": tick.last_price,
                    "volume": tick.volume_traded,
                    "oi": tick.oi or 0,
                    "buy_quantity": tick.total_buy_quantity,
                    "sell_quantity": tick.total_sell_quantity,
                    "open": tick.ohlc.open,
                    "high": tick.ohlc.high,
                    "low": tick.ohlc.low,
                    "close": tick.ohlc.close,
                    "net_change": tick.change,
                })
                self._last_quote_persist[tick.instrument_token] = now_mono

            if tick.instrument_token == NIFTY_TOKEN:
                self._oi_tracker.set_spot_price("NIFTY", tick.last_price)
            elif tick.instrument_token == SENSEX_TOKEN:
                self._oi_tracker.set_spot_price("SENSEX", tick.last_price)

            if tick.oi is not None or tick.oi_day_high is not None:
                self._oi_tracker.update_from_tick({
                    "instrument_token": tick.instrument_token,
                    "last_price": tick.last_price,
                    "volume_traded": tick.volume_traded,
                    "change": tick.change,
                    "oi": tick.oi or 0,
                    "oi_day_high": tick.oi_day_high or 0,
                    "oi_day_low": tick.oi_day_low or 0,
                })

            ts = (tick.exchange_timestamp or datetime.now()).replace(tzinfo=None)
            for iv, agg in self._bar_aggs.items():
                if (iv, tick.instrument_token) in self._live_bars:
                    for bar in agg.update(tick.instrument_token, tick.last_price, tick.volume_traded, ts):
                        completed_bars.append((iv, tick.instrument_token, bar))

        if quote_rows:
            try:
                self._market_store.store_quote_snapshots(quote_rows)
            except Exception as e:
                logger.error("quote_snapshot_store_failed", error=str(e))

        for iv, token, bar in completed_bars:
            await self._on_bar_close(iv, token, bar)

        # Bar strategies signal on bar close (above). Only strategies that
        # explicitly opt in also see raw ticks.
        for strategy in self._strategies:
            if not strategy.is_active or not getattr(strategy, "signal_on_tick", False):
                continue
            try:
                signals = await strategy.on_tick(ticks)
                self._emit("strategy.tick_eval", strategy=strategy.name, ticks=len(ticks), signals=len(signals))
                for sig in signals:
                    await self._process_signal(sig)
            except Exception as e:
                logger.error("strategy_tick_error", strategy=strategy.name, error=str(e))
                self._emit("strategy.error", strategy=strategy.name, interval="tick", error=str(e))

        await self._broadcast_ws({
            "type": "ticks",
            "data": self._live_ticks,
        })

    def _emit(self, topic: str, **data: Any) -> None:
        """Publish workload telemetry for the agents; a no-op when no bus is
        attached (e.g. services built without __init__ in tests). Never raises."""
        bus = self.__dict__.get("telemetry")
        if bus is not None:
            bus.emit(topic, **data)

    def _symbol_for(self, token: int, strategy: Any = None) -> str:
        """Tradingsymbol for a token, from the strategies' symbol maps (for logs)."""
        for s in ([strategy] if strategy is not None else []) + list(self._strategies):
            tmap = (getattr(s, "params", None) or {}).get("tradingsymbol_map") or {}
            name = tmap.get(token) or tmap.get(str(token))
            if name:
                return name
        return str(token)

    async def _on_bar_close(self, interval: str, token: int, bar: Any) -> None:
        """Append a completed bar to that timeframe's bars and let the strategies
        running on that timeframe evaluate it."""
        row = bar.as_row()
        df = self._live_bars.get((interval, token))
        if df is None:
            return
        if not df.empty and str(df["timestamp"].iloc[-1])[:16] == row["timestamp"][:16]:
            df = df.iloc[:-1]
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True).tail(self.MAX_LIVE_BARS).reset_index(drop=True)
        self._live_bars[(interval, token)] = df
        series = df.iloc[-1]
        self._emit("bar.close", interval=interval, symbol=self._symbol_for(token), close=row.get("close"))
        for strategy in self._strategies:
            if self.live_interval(strategy) != interval:
                continue
            strategy.update_bar_data(token, df)
            if not strategy.is_active:
                continue
            symbol = self._symbol_for(token, strategy)
            started = time.perf_counter()
            try:
                signals = await strategy.on_bar(token, series)
                self._emit(
                    "strategy.eval", strategy=strategy.name, symbol=symbol, interval=interval,
                    close=row.get("close"), signals=len(signals),
                    ms=round((time.perf_counter() - started) * 1000, 1),
                )
                for sig in signals:
                    await self._process_signal(sig)
            except Exception as e:
                logger.error("strategy_bar_error", strategy=strategy.name, token=token, error=str(e))
                self._emit("strategy.error", strategy=strategy.name, symbol=symbol, interval=interval, error=str(e))

    BAR_FLUSH_GRACE_S = 2.0   # ticks stamped inside a bar can arrive just after it ends

    async def _bar_flush_loop(self) -> None:
        """Close bars on time for instruments that stop ticking."""
        while self._running:
            await asyncio.sleep(1.0)
            now = datetime.now() - timedelta(seconds=self.BAR_FLUSH_GRACE_S)
            for iv, agg in list(self._bar_aggs.items()):
                for token, bar in agg.flush(now):
                    try:
                        await self._on_bar_close(iv, token, bar)
                    except Exception as e:
                        logger.error("bar_flush_error", token=token, interval=iv, error=str(e))

    async def _on_connect(self) -> None:
        logger.info("ticker_connected")
        await self._broadcast_ws({"type": "status", "data": {"connected": True}})

    async def _on_close(self, code: int, reason: str) -> None:
        logger.warning("ticker_closed", code=code, reason=reason)
        await self._broadcast_ws({"type": "status", "data": {"connected": False}})

    async def _on_error(self, error: Exception) -> None:
        logger.error("ticker_error", error=str(error))

    async def _on_order_update(self, data: dict[str, Any]) -> None:
        self._journal.record_trade(
            strategy="live",
            tradingsymbol=data.get("tradingsymbol", ""),
            exchange=data.get("exchange", ""),
            transaction_type=data.get("transaction_type", ""),
            quantity=data.get("quantity", 0),
            price=data.get("average_price", 0),
            order_id=data.get("order_id", ""),
            status=data.get("status", ""),
        )
        await self._broadcast_ws({"type": "order_update", "data": data})

    # ────────────────────────────────────────────────────────────
    # Execution → Journal Callbacks
    # ────────────────────────────────────────────────────────────

    async def _on_execution_fill(self, order_id: str, broker_order: Any, signal: Any) -> None:
        """Called by ExecutionEngine when an entry order fills.
        Records fill in both TradeJournal and JournalStore (pro journal).
        """
        fill_price = getattr(broker_order, "average_price", 0) or 0.0
        tradingsymbol = getattr(broker_order, "tradingsymbol", "") or (signal.tradingsymbol if signal else "")
        exchange = getattr(broker_order, "exchange", "") or (signal.exchange.value if signal and hasattr(signal.exchange, "value") else "")
        txn_type = getattr(broker_order, "transaction_type", "") or (signal.transaction_type.value if signal else "")
        qty = getattr(broker_order, "filled_quantity", 0) or (signal.quantity if signal else 0)
        strategy_name = signal.strategy_name if signal else "live"

        # 1. Record in simple TradeJournal (JSON file)
        self._journal.record_trade(
            strategy=strategy_name,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            transaction_type=txn_type,
            quantity=qty,
            price=fill_price,
            order_id=order_id,
            status="COMPLETE",
            metadata=signal.metadata if signal else {},
        )

        # 2. Record in JournalStore (SQLite pro journal)
        try:
            from src.journal.journal_models import (
                JournalEntry, ExecutionRecord, StrategyContext, TradeDirection
            )
            direction = TradeDirection.LONG if txn_type == "BUY" else TradeDirection.SHORT
            exec_rec = ExecutionRecord(
                trade_id=order_id,
                instrument=tradingsymbol,
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                direction=txn_type,
                quantity=qty,
                actual_fill_price=fill_price,
                expected_entry_price=signal.price if signal and signal.price else fill_price,
                order_type=signal.order_type.value if signal and hasattr(signal.order_type, "value") else "MARKET",
            )
            strat_ctx = StrategyContext(
                signal_confidence=signal.confidence if signal else 0,
            )
            entry = JournalEntry(
                trade_id=order_id,
                instrument=tradingsymbol,
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                direction=direction.value if hasattr(direction, 'value') else str(direction),
                strategy_name=strategy_name,
                source="execution_engine",
                entry_time=datetime.now().isoformat(),
                execution=exec_rec.to_dict(),
                strategy_context=strat_ctx.to_dict(),
                tags=["auto", strategy_name],
            )
            self._journal_store.record_entry(entry)
            logger.info("journal_entry_recorded_on_fill", order_id=order_id, symbol=tradingsymbol)
        except Exception as e:
            logger.error("journal_store_fill_error", error=str(e), order_id=order_id)

        # 3. Notify portfolio health tracker
        try:
            trade_value = fill_price * qty
            self._portfolio_health.on_trade_open(
                trade_value=trade_value,
                symbol=tradingsymbol,
                strategy=strategy_name,
            )
        except Exception as e:
            logger.error("portfolio_health_open_error", error=str(e))

        logger.info(
            "execution_fill_journaled",
            order_id=order_id,
            symbol=tradingsymbol,
            price=fill_price,
            strategy=strategy_name,
        )

    async def _on_execution_exit(self, tracked: Any, exit_order_id: str) -> None:
        """Called by ExecutionEngine when a position exits (SL, trailing, MIS squareoff, manual).
        Records the exit in TradeJournal and updates JournalStore.
        """
        exit_reason = getattr(tracked, "exit_reason", "unknown")
        
        # Record in simple TradeJournal
        exit_txn = "SELL" if tracked.is_long else "BUY"
        self._journal.record_trade(
            strategy=tracked.strategy_name,
            tradingsymbol=tracked.tradingsymbol,
            exchange=tracked.exchange,
            transaction_type=exit_txn,
            quantity=tracked.quantity,
            price=tracked.exit_price or 0.0,
            order_id=exit_order_id or "",
            status="EXIT",
            pnl=tracked.pnl,
            notes=f"Exit reason: {exit_reason}",
        )

        # Update risk PnL
        try:
            self._risk.update_daily_pnl(tracked.pnl)
        except Exception as e:
            logger.error("risk_pnl_update_error", error=str(e))

        # Notify portfolio health tracker
        try:
            self._portfolio_health.on_trade_close(
                pnl=tracked.pnl,
                symbol=tracked.tradingsymbol,
                strategy=tracked.strategy_name,
            )
        except Exception as e:
            logger.error("portfolio_health_close_error", error=str(e))

        # Broadcast exit to dashboard
        await self._broadcast_ws({
            "type": "position_exit",
            "data": {
                "order_id": tracked.order_id,
                "symbol": tracked.tradingsymbol,
                "direction": "LONG" if tracked.is_long else "SHORT",
                "entry_price": tracked.entry_price,
                "exit_price": tracked.exit_price,
                "pnl": tracked.pnl,
                "reason": exit_reason,
                "exit_order_id": exit_order_id,
            },
        })

        logger.info(
            "execution_exit_journaled",
            symbol=tracked.tradingsymbol,
            reason=exit_reason,
            pnl=tracked.pnl,
            exit_order=exit_order_id,
        )

        # Echo the closed trade into the Signals feed for promoted/live equity
        # strategies too (not just OI) so a completed trade round-trips into the
        # Signals tab the user is watching. OI positions already append their own
        # signal entry below, so skip those to avoid a duplicate.
        if not tracked.strategy_name.startswith("oi_strategy_"):
            exit_signal_data = {
                "timestamp": datetime.now().isoformat(),
                "strategy": tracked.strategy_name,
                "tradingsymbol": tracked.tradingsymbol,
                "transaction_type": exit_txn,
                "quantity": tracked.quantity,
                "price": tracked.exit_price or 0.0,
                "confidence": 0,
                "metadata": {
                    "action": "exit", "order_id": exit_order_id,
                    "entry_price": tracked.entry_price, "pnl": tracked.pnl,
                    "reason": exit_reason,
                },
            }
            self._signal_log.append(exit_signal_data)
            await self._broadcast_ws({"type": "signal", "data": exit_signal_data})

        # If this was an OI-strategy position, sync the OIStrategyEngine's own
        # position record + its journal entry + Signals feed - whether the exit
        # was triggered by a broker-side SL/target fill, manual close, or MIS
        # square-off, there's exactly one real order behind it (no duplicates).
        if tracked.strategy_name.startswith("oi_strategy_"):
            signal = getattr(tracked, "signal", None)
            position_id = (signal.metadata.get("position_id") if signal else None)
            if position_id:
                pos = self._oi_strategy.close_position(position_id, tracked.exit_price or 0.0, exit_reason)
                if pos:
                    pos.metadata["exit_order_id"] = exit_order_id
                    self._record_oi_journal_entry(pos, "EXIT")
                    oi_signal_data = {
                        "timestamp": pos.exit_time or datetime.now().isoformat(),
                        "strategy": tracked.strategy_name,
                        "tradingsymbol": pos.tradingsymbol,
                        "transaction_type": exit_txn,
                        "quantity": pos.quantity,
                        "price": pos.exit_price,
                        "confidence": 0,
                        "metadata": {"position_id": pos.id, "pnl": pos.pnl, "underlying": pos.underlying, "order_id": exit_order_id, "reason": exit_reason},
                    }
                    self._signal_log.append(oi_signal_data)
                    await self._broadcast_ws({"type": "signal", "data": oi_signal_data})
                    logger.info("oi_position_closed", position_id=position_id, order_id=exit_order_id, reason=exit_reason, pnl=pos.pnl)
                else:
                    logger.warning("oi_position_close_sync_failed", position_id=position_id, reason="already closed or not found")

    async def _process_signal(self, signal: Signal) -> Optional[str]:
        """Route one strategy signal to the execution engine. Returns the order id
        when an order was placed (None when ignored, blocked or failed)."""
        # Same rules as backtest / paper (src/data/exit_plan.py): the timeframe
        # sets the product (intraday MIS, daily CNC long-only), an opposite
        # signal closes the strategy's open position (reversing where allowed),
        # and strategies with their own exit plans are only closed by the plan.
        strategy = next((s for s in self._strategies if s.name == signal.strategy_name), None)
        timeframe = self.live_interval(strategy) if strategy else (signal.metadata or {}).get("timeframe", "5minute")
        signal.metadata = {**(signal.metadata or {}), "timeframe": timeframe}
        signal.product = ProductType(product_for(timeframe))
        side = 1 if signal.transaction_type == TransactionType.BUY else -1
        is_exit = signal.metadata.get("action") == "exit"
        can_open = not is_exit and (side > 0 or allows_short(timeframe))
        sig_info = {
            "strategy": signal.strategy_name, "symbol": signal.tradingsymbol,
            "side": signal.transaction_type.value, "quantity": signal.quantity,
            "price": signal.price, "timeframe": timeframe, "confidence": signal.confidence,
        }
        self._emit("signal.received", **sig_info, exit=is_exit)
        open_pos = self._execution.open_position_for(signal.tradingsymbol, signal.strategy_name)
        if open_pos is not None:
            # a signal carrying its own exit plan (metadata["exec"]) is managed by that plan
            plan_managed = getattr(strategy, "uses_exit_plans", False) or bool(signal.metadata.get("exec"))
            if open_pos.is_long == (side > 0) or plan_managed:
                logger.info("signal_ignored_position_open", strategy=signal.strategy_name, symbol=signal.tradingsymbol)
                self._emit("signal.ignored", **sig_info, reason="position already open in that direction")
                return
            await self._execution.close_position(open_pos.order_id, reason="signal_exit")
            self._emit("order.close", **sig_info, order_id=open_pos.order_id, reason="opposite signal")
        if not can_open:
            if not is_exit:
                self._emit("signal.ignored", **sig_info, reason=f"shorting not allowed on {timeframe} (delivery)")
            return

        signal_data = {
            "timestamp": datetime.now().isoformat(),
            "strategy": signal.strategy_name,
            "tradingsymbol": signal.tradingsymbol,
            "transaction_type": signal.transaction_type.value,
            "quantity": signal.quantity,
            "price": signal.price,
            "confidence": signal.confidence,
            "metadata": signal.metadata,
        }
        self._signal_log.append(signal_data)
        await self._broadcast_ws({"type": "signal", "data": signal_data})

        try:
            order_id = await self._execution.execute_signal(signal)
            if order_id:
                self._emit("order.placed", **sig_info, order_id=order_id)
            else:
                self._emit("order.failed", **sig_info, error="execution engine returned no order id")
            if order_id:
                self._journal.record_trade(
                    strategy=signal.strategy_name,
                    tradingsymbol=signal.tradingsymbol,
                    exchange=signal.exchange.value,
                    transaction_type=signal.transaction_type.value,
                    quantity=signal.quantity,
                    price=signal.price or 0.0,
                    order_id=order_id,
                    status="PLACED",
                    metadata=signal.metadata,
                )
        except KillSwitchError:
            logger.critical("kill_switch_triggered")
            self._emit("risk.kill_switch", **sig_info)
            for s in self._strategies:
                s.deactivate()
        except RiskLimitError as e:
            logger.warning("risk_limit_hit", error=str(e))
            self._emit("risk.blocked", **sig_info, error=str(e))
        except Exception as e:
            logger.error("signal_processing_error", error=str(e))
            self._emit("order.failed", **sig_info, error=str(e))
            return None
        return order_id or None

    # ────────────────────────────────────────────────────────────
    # Paper Trading
    # ────────────────────────────────────────────────────────────

    async def run_paper_trade(
        self,
        strategy_name: str,
        instrument_token: int,
        tradingsymbol: str,
        exchange: str = "NSE",
        days: int = 60,
        interval: str = "5minute",
        capital: float = 100000.0,
        commission_pct: float = 0.03,
        slippage_pct: float = 0.05,
        strategy_params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Run paper trade with a built-in or custom strategy on historical data."""
        # Resolve strategy
        strategy = self._resolve_strategy(strategy_name, strategy_params)
        if isinstance(strategy, dict):  # error dict
            return strategy
        interval = getattr(strategy, "required_interval", None) or interval

        # Extend fetch with warm-up history so the 200-DMA regime filter is
        # active across the whole requested window (see _with_dma200_warmup).
        fetch_days = self._with_dma200_warmup(strategy, interval, days, None)

        # Fetch historical data (DB cache first, live Kite fetch as fallback)
        try:
            rows = await self._resolve_ohlcv_rows(instrument_token, interval, days=fetch_days)
        except Exception as e:
            logger.error("paper_trade_data_fetch_failed", error=str(e))
            return {"error": f"Failed to fetch data: {str(e)}"}

        if not rows:
            return {"error": "No historical data returned. Check instrument token and date range."}

        df = pd.DataFrame(rows).rename(columns={"ts": "timestamp"})

        # Run paper trade
        # NOTE: commission_pct/slippage_pct are no longer accepted by PaperTradingEngine -
        # it now derives realistic Indian brokerage + market-impact slippage internally.
        engine = PaperTradingEngine(
            strategy=strategy,
            initial_capital=capital,
        )
        result = await asyncio.to_thread(engine.run,
            data=df,
            instrument_token=instrument_token,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            timeframe=interval,
        )

        result_dict = result.to_dict_safe()
        result_dict["generated_at"] = datetime.now().isoformat()
        self._paper_results[strategy_name] = result_dict

        # Compute strategy health report and embed in result
        try:
            health = self.compute_strategy_health(result_dict, strategy_type="equity", strategy_name=strategy_name)
            result_dict["health_report"] = health
        except Exception as e:
            logger.error("health_compute_error", error=str(e))

        # Record paper trades to journal for analytics
        try:
            self._journal_record_paper_trades(result_dict, strategy_name, tradingsymbol)
        except Exception as e:
            logger.error("paper_trade_journal_record_error", error=str(e))

        logger.info("paper_trade_complete", strategy=strategy_name, trades=result.total_trades)
        return result_dict

    async def run_paper_trade_with_sample_data(
        self,
        strategy_name: str,
        tradingsymbol: str = "SAMPLE",
        bars: int = 500,
        capital: float = 100000.0,
        strategy_params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Run paper trade with synthetic data (no auth required)."""
        import numpy as np

        strategy = self._resolve_strategy(strategy_name, strategy_params)
        if isinstance(strategy, dict):
            return strategy

        # Generate realistic synthetic OHLCV data with trending regimes
        np.random.seed(42)
        dates = pd.date_range("2024-01-01", periods=bars, freq="5min")
        price = 100.0
        opens, highs, lows, closes, volumes = [], [], [], [], []
        regime_length = max(20, bars // 10)
        trend = 0.0
        vol_scale = 0.5
        for i in range(bars):
            # Switch regime periodically for trending & ranging moves
            if i % regime_length == 0:
                trend = np.random.choice([-0.15, -0.05, 0.0, 0.05, 0.15])
                vol_scale = np.random.choice([0.3, 0.5, 0.8])
            change = np.random.normal(trend, vol_scale)
            o = price
            c = price + change
            h = max(o, c) + abs(np.random.normal(0, vol_scale * 0.4))
            l = min(o, c) - abs(np.random.normal(0, vol_scale * 0.4))
            # Volume with occasional spikes
            v = int(np.random.uniform(20000, 80000))
            if np.random.random() < 0.15:
                v = int(v * np.random.uniform(2.0, 4.0))
            opens.append(o)
            highs.append(h)
            lows.append(l)
            closes.append(c)
            volumes.append(v)
            price = c

        df = pd.DataFrame({
            "timestamp": dates, "open": opens, "high": highs,
            "low": lows, "close": closes, "volume": volumes,
        })

        engine = PaperTradingEngine(strategy=strategy, initial_capital=capital)
        result = await asyncio.to_thread(engine.run, df, instrument_token=0, tradingsymbol=tradingsymbol, timeframe="5min")
        result_dict = result.to_dict_safe()
        result_dict["generated_at"] = datetime.now().isoformat()
        self._paper_results[strategy_name] = result_dict

        # Compute strategy health report and embed in result
        try:
            health = self.compute_strategy_health(result_dict, strategy_type="equity", strategy_name=strategy_name)
            result_dict["health_report"] = health
        except Exception as e:
            logger.error("health_compute_error", error=str(e))

        # Record paper trades to journal
        try:
            self._journal_record_paper_trades(result_dict, strategy_name, tradingsymbol)
        except Exception as e:
            logger.error("paper_trade_sample_journal_error", error=str(e))

        return result_dict

    def get_paper_results(self) -> dict[str, Any]:
        return self._paper_results.copy()

    def _resolve_strategy(
        self, name: str, params: Optional[dict[str, Any]] = None,
    ) -> BaseStrategy | dict[str, Any]:
        """Get a strategy instance by name (built-in or custom)."""
        strategy_map = self._equity_strategy_classes()

        if name in strategy_map:
            return strategy_map[name](params)

        # Check custom strategies
        custom = self._custom_store.build_strategy(name)
        if custom:
            return custom

        return {
            "error": f"Unknown strategy: {name}",
            "available": list(strategy_map.keys()) + [
                s["name"] for s in self._custom_store.list_strategies()
            ],
        }

    # ────────────────────────────────────────────────────────────
    # Custom Strategy Builder
    # ────────────────────────────────────────────────────────────

    def get_custom_strategy_store(self) -> CustomStrategyStore:
        return self._custom_store

    def save_custom_strategy(self, config: dict[str, Any]) -> dict[str, Any]:
        return self._custom_store.save_strategy(config)

    def delete_custom_strategy(self, name: str) -> dict[str, Any]:
        return self._custom_store.delete_strategy(name)

    def list_custom_strategies(self) -> list[dict[str, Any]]:
        return self._custom_store.list_strategies()

    def get_custom_strategy(self, name: str) -> Optional[dict[str, Any]]:
        return self._custom_store.get_strategy(name)

    def get_builder_indicators(self) -> list[dict[str, Any]]:
        return get_available_indicators()

    # ────────────────────────────────────────────────────────────
    # F&O Custom Strategy Builder
    # ────────────────────────────────────────────────────────────

    def save_fno_custom_strategy(self, config: dict[str, Any]) -> dict[str, Any]:
        cfg = FnOStrategyConfig.from_dict(config)
        if not cfg.name:
            return {"error": "Strategy name is required"}
        if not cfg.legs:
            return {"error": "At least one leg is required"}
        # Auto-set lot size
        cfg.lot_size = UNDERLYING_LOT_SIZES.get(cfg.underlying, 50)
        return self._fno_strategy_store.save(cfg)

    def delete_fno_custom_strategy(self, name: str) -> dict[str, Any]:
        return self._fno_strategy_store.delete(name)

    def list_fno_custom_strategies(self) -> list[dict[str, Any]]:
        return self._fno_strategy_store.list_all()

    def get_fno_custom_strategy(self, name: str) -> Optional[dict[str, Any]]:
        cfg = self._fno_strategy_store.get(name)
        return cfg.to_dict() if cfg else None

    def get_fno_builder_templates(self) -> list[dict[str, Any]]:
        return self._fno_strategy_store.get_templates()

    def compute_fno_payoff(self, config: dict[str, Any], spot_price: float) -> dict[str, Any]:
        """Compute payoff diagram for a custom F&O strategy."""
        cfg = FnOStrategyConfig.from_dict(config)
        cfg.lot_size = UNDERLYING_LOT_SIZES.get(cfg.underlying, 50)
        resolved = resolve_legs(cfg, spot_price, chain=None)
        return compute_strategy_payoff(resolved, spot_price, cfg.lot_size)

    def get_analysis_strategy_list(self) -> list[dict[str, Any]]:
        """Return list of all individual analysis strategies for UI dropdowns."""
        from src.strategy.analysis_strategies import ANALYSIS_STRATEGIES, ANALYSIS_STRATEGY_LABELS
        return [
            {"value": k, "label": v}
            for k, v in ANALYSIS_STRATEGY_LABELS.items()
        ]

    async def run_analysis_scan(self, symbols: Optional[list[str]] = None,
                                universe: str = "nifty50") -> dict[str, Any]:
        async def _on_progress(scanned: int, total: int, symbol: str) -> None:
            self._emit("scan.progress", universe=universe, scanned=scanned, total=total, symbol=symbol)
            await self._broadcast_ws({
                "type": "scan_progress",
                "data": {"scanned": scanned, "total": total, "symbol": symbol},
            })

        self._emit("scan.started", universe=universe)
        result = await self._scanner.run_scan(
            self._client, symbols, universe=universe, on_progress=_on_progress
        )
        self._emit("scan.completed", universe=universe, report=result)
        self._snapshot_dirty = True
        await self.persist_analysis(force=True)
        return result

    def get_analysis_results(self, universe: str = "nifty50") -> dict[str, Any]:
        return self._scanner.get_cached_results(universe)

    def get_scanner_status(self) -> dict[str, Any]:
        return {
            "scanning": self._scanner.is_scanning,
            "last_scan_time": self._scanner.last_scan_time,
            "has_data": bool(self._scanner._analysis_cache),
        }

    def get_deep_profile(self, symbol: str) -> dict[str, Any]:
        """Get deep analysis profile for a single stock."""
        return self._scanner.get_deep_profile(symbol)

    async def analyze_stock_on_demand(self, symbol: str, force: bool = False) -> dict[str, Any]:
        """Deep profile for any NSE stock: cached if analysed today, else refreshed now."""
        result = await self._scanner.analyze_stock_on_demand(self._client, symbol, force=force)
        if result.get("cache") == "refreshed":
            self._snapshot_dirty = True
            await self.persist_analysis()
        return result

    # ── analysis cache persistence & warm-up ──────────────────────────

    def refresh_analysis_in_background(self, symbol: str) -> None:
        """Re-analyse ``symbol`` without blocking the caller (one task per symbol)."""
        tasks = self._refresh_tasks
        if symbol in tasks and not tasks[symbol].done():
            return
        task = asyncio.create_task(self.analyze_stock_on_demand(symbol, force=True), name=f"refresh_{symbol}")
        task.add_done_callback(self._task_exception_handler)
        task.add_done_callback(lambda t: tasks.pop(symbol, None))
        tasks[symbol] = task

    def _analysis_snapshot_path(self) -> str:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base, "data", "scanner_cache", f"{self._zerodha_id or 'default'}.json.gz")

    async def persist_analysis(self, min_interval_s: float = 60.0, force: bool = False) -> bool:
        """Write the analysis snapshot in a worker thread, at most every ``min_interval_s``."""
        if not (force or self._snapshot_dirty):
            return False
        if not force and time.monotonic() - self._snapshot_saved_mono < min_interval_s:
            return False
        self._snapshot_saved_mono = time.monotonic()
        self._snapshot_dirty = False
        try:
            await asyncio.to_thread(self._scanner.save_snapshot, self._analysis_snapshot_path())
            return True
        except Exception as e:
            self._snapshot_dirty = True
            logger.warning("scanner_snapshot_save_failed", error=str(e))
            return False

    _DERIVATIVE_RE = re.compile(
        r"^(.+?)(\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)|\d{2}[1-9OND]\d{2})(?:\d+(?:\.\d+)?(?:CE|PE)|FUT)$"
    )
    _INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}

    async def portfolio_symbols(self) -> list[str]:
        """Stocks the user holds or has positions in (derivatives mapped to their underlying)."""
        symbols: list[str] = []
        try:
            for h in await self.get_holdings():
                symbols.append(str(h.get("tradingsymbol") or ""))
        except Exception as e:
            logger.warning("portfolio_symbols_holdings_failed", error=str(e))
        try:
            for p in (await self.get_positions()).get("net", []):
                sym = str(p.get("tradingsymbol") or "")
                m = self._DERIVATIVE_RE.match(sym)
                symbols.append(m.group(1) if m else sym)
        except Exception as e:
            logger.warning("portfolio_symbols_positions_failed", error=str(e))
        out = []
        for s in symbols:
            s = re.sub(r"-(EQ|BE|BZ|SM|ST)$", "", s.upper().strip())
            if s and s not in self._INDEX_UNDERLYINGS and s not in out:
                out.append(s)
        return out

    async def warm_portfolio_analysis(self) -> dict[str, Any]:
        """Analyse held / traded stocks that aren't fresh in the cache (one at a time)."""
        symbols = await self.portfolio_symbols()
        stale = [s for s in symbols if not self._scanner.is_fresh(s)]
        refreshed, failed = [], []
        for sym in stale:
            res = await self._scanner.analyze_stock_on_demand(self._client, sym)
            (failed if res.get("error") else refreshed).append(sym)
        if refreshed:
            self._snapshot_dirty = True
            await self.persist_analysis(force=True)
        return {"symbols": symbols, "refreshed": refreshed, "failed": failed}

    def get_sector_analysis(self, universe: str | None = None) -> dict[str, Any]:
        """Get sector-level analysis from cached scan data."""
        return self._scanner.get_sector_analysis(universe)

    # ────────────────────────────────────────────────────────────
    # Historical Chart Data
    # ────────────────────────────────────────────────────────────

    async def get_historical_chart(
        self, instrument_token: int, interval: str = "day", days: int = 365,
        from_date: str | None = None, to_date: str | None = None,
        include_indicators: bool = False,
    ) -> dict[str, Any]:
        return await self._market_data.get_historical_chart(
            instrument_token, interval, days, from_date, to_date, include_indicators
        )

    async def get_historical_df(
        self, instrument_token: int, interval: str = "day", days: int = 365,
        from_date: str | None = None, to_date: str | None = None,
    ) -> Any:
        return await self._market_data.get_historical_df(
            instrument_token, interval, days, from_date, to_date
        )

    # ────────────────────────────────────────────────────────────
    # Live Market Quotes
    # ────────────────────────────────────────────────────────────

    async def get_market_quote(self, instruments: list[str]) -> dict[str, Any]:
        return await self._market_data.get_quote(instruments)

    async def get_market_ltp(self, instruments: list[str]) -> dict[str, Any]:
        return await self._market_data.get_ltp(instruments)

    async def get_market_ohlc(self, instruments: list[str]) -> dict[str, Any]:
        return await self._market_data.get_ohlc(instruments)

    async def get_market_overview(self, symbols: list[str] | None = None) -> dict[str, Any]:
        return await self._market_data.get_market_overview(symbols)

    async def resolve_instrument_token(self, symbol: str, exchange: str = "NSE") -> int | None:
        return await self._market_data.resolve_token(symbol, exchange)

    # ── F&O market flow ─────────────────────────────────────────

    def _fno_baseline_path(self, day: str) -> str:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base, "data", f"fno_baseline_{self._zerodha_id or 'default'}_{day}.json")

    async def _ensure_fno_universe(self) -> None:
        today = datetime.now().date()
        if self._fno_universe and self._fno_universe_date == today.isoformat():
            return
        nfo = await self._client.get_instruments("NFO")
        nse = await self._client.get_instruments("NSE")
        self._fno_universe = build_universe(nfo, nse, today, get_sector, cash_only_symbols=NIFTY_500_SYMBOLS)
        self._fno_universe_date = today.isoformat()
        path = self._fno_baseline_path(self._fno_universe_date)
        try:
            with open(path, encoding="utf-8") as f:
                self._fno_baselines = json.load(f)
        except (OSError, ValueError):
            self._fno_baselines = {}
        logger.info("fno_universe_loaded", stocks=len(self._fno_universe), baselines=len(self._fno_baselines))

    async def _load_fno_baselines(self) -> None:
        """Fetch previous-session cash candles and futures OI for every F&O
        stock (2 historical calls each, throttled by the client limiter)."""
        today = datetime.now().date()
        frm = (today - timedelta(days=40)).isoformat()
        to = today.isoformat()
        path = self._fno_baseline_path(self._fno_universe_date)
        pending = [c for c in self._fno_universe if c.symbol not in self._fno_baselines]

        async def load(c: Any) -> None:
            try:
                cash = await self._client.get_historical_data(c.cash_token, Interval.DAY, frm, to)
                fut = (
                    await self._client.get_historical_data(c.fut_token, Interval.DAY, frm, to, oi=True)
                    if c.has_future else []
                )
            except Exception as e:
                logger.warning("fno_baseline_failed", symbol=c.symbol, error=str(e))
                return
            self._fno_baselines[c.symbol] = {
                "cash": [[str(k.timestamp)[:10], k.high, k.low, k.close, k.volume] for k in cash],
                "fut_oi": [[str(k.timestamp)[:10], k.oi or 0] for k in fut],
            }

        for i in range(0, len(pending), 6):
            await asyncio.gather(*(load(c) for c in pending[i:i + 6]))
            if (i // 6) % 10 == 9 or i + 6 >= len(pending):
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(self._fno_baselines, f)
                except OSError as e:
                    logger.warning("fno_baseline_save_failed", error=str(e))
        logger.info("fno_baselines_loaded", count=len(self._fno_baselines))

    async def get_fno_flow(self, max_age_s: float = 2.0) -> dict[str, Any]:
        """Market-flow snapshot for F&O + NIFTY 500 (cash, plus nearest future for F&O stocks)."""
        async with self._fno_lock:
            cached = self._fno_cache
            if cached and time.monotonic() - cached[0] < max_age_s:
                return cached[1]
            try:
                await self._ensure_fno_universe()
            except Exception as e:
                return {"error": f"instrument load failed: {e}"}
            if not self._fno_universe:
                return {"error": "no F&O stock futures found in NFO instruments"}
            if len(self._fno_baselines) < len(self._fno_universe) and (
                self._fno_baseline_task is None or self._fno_baseline_task.done()
            ):
                self._fno_baseline_task = asyncio.create_task(self._load_fno_baselines(), name="fno_baselines")
                self._fno_baseline_task.add_done_callback(self._task_exception_handler)

            keys = quote_keys(self._fno_universe)
            quotes: dict[str, Any] = {}
            for i in range(0, len(keys), 500):
                resp = await self._market_data.get_quote(keys[i:i + 500])
                if "error" in resp:
                    return {"error": resp["error"]}
                quotes.update(resp.get("quotes", {}))

            now = datetime.now()
            result = compute_fno_flow(
                self._fno_universe, quotes, self._fno_baselines, now, self._fno_history.changes_ago(15, now),
            )
            result["baseline_loading"] = bool(self._fno_baseline_task and not self._fno_baseline_task.done())
            self._fno_history.record(result, now)
            self._fno_cache = (time.monotonic(), result)
            return result

    def get_fno_flow_history(self, minutes: int = 60) -> list[dict[str, Any]]:
        return self._fno_history.since(minutes, datetime.now())

    # ── 5-minute day-trade tracking ───────────────────────────
    DAY_TRADE_POLL_S = 10.0
    DAY_TRADE_IDLE_S = 120.0   # outside market hours, stop once nobody is watching

    async def _fetch_day_bars(self, symbol: str) -> list[dict[str, Any]]:
        """5-minute cash candles: the latest session plus enough earlier sessions for
        the setups' ATR / previous-day levels (same inputs as backtests)."""
        contract = next((c for c in self._fno_universe if c.symbol == symbol), None)
        if contract is None:
            return []
        from src.strategy.day_trade_setups import HISTORY_CALENDAR_DAYS

        today = datetime.now().date()
        candles = await self._client.get_historical_data(
            contract.cash_token, Interval.MINUTE_5,
            (today - timedelta(days=HISTORY_CALENDAR_DAYS)).isoformat(), today.isoformat(),
        )
        bars = [
            {"t": str(k.timestamp), "o": k.open, "h": k.high, "l": k.low, "c": k.close, "v": k.volume}
            for k in candles
        ]
        try:   # keep the bars, so board sessions (and filter / exit changes) can be replayed later
            self._market_store.store_candles(contract.cash_token, "5minute", [
                {"ts": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": b["v"]} for b in bars
            ])
        except Exception as e:
            logger.warning("day_trade_bars_store_failed", symbol=symbol, error=str(e))
        return bars

    async def _day_trade_loop(self) -> None:
        """Keep the tracked list and its bars current through the session."""
        while self.is_authenticated:
            now = datetime.now()
            idle = time.monotonic() - self._day_trade_last_request > self.DAY_TRADE_IDLE_S
            # runs all session while the market is open (journal + opt-in auto-trade need no viewer)
            if idle and not market_open(now):
                break
            try:
                flow = await self.get_fno_flow(max_age_s=5.0)
                if not flow.get("error"):
                    self._day_trade.update_selection(flow, now)
                    await self._day_trade.refresh(now)
                    # journal every signal / status change and (if opted in) act on new ones,
                    # whether or not anyone has the board open
                    snap = self._day_trade.snapshot(flow, now)
                    await self._board.process(snap, now, send=self._send_board_signal)
                    self._board.save(self._day_trade.to_state(), now)
            except Exception as e:
                logger.warning("day_trade_refresh_failed", error=str(e))
            await asyncio.sleep(self.DAY_TRADE_POLL_S)

    def _ensure_day_trade_loop(self) -> None:
        self._day_trade_last_request = time.monotonic()
        if self._day_trade_task is None or self._day_trade_task.done():
            self._day_trade_task = asyncio.create_task(self._day_trade_loop(), name="day_trade")
            self._day_trade_task.add_done_callback(self._task_exception_handler)

    async def get_day_trade(self) -> dict[str, Any]:
        """Tracked symbols with 5-minute bars, setups, open trades and the signal feed."""
        self._ensure_day_trade_loop()
        flow = await self.get_fno_flow(max_age_s=5.0)
        if flow.get("error"):
            return {"error": flow["error"]}
        now = datetime.now()
        self._day_trade.update_selection(flow, now)
        snap = self._day_trade.snapshot(flow, now)
        await self._board.process(snap, now)          # journal only; orders are sent by the loop
        self._board.save(self._day_trade.to_state(), now)
        snap = self._board.annotate(snap)
        snap["autotrade"]["live_trading"] = self._running and bool(getattr(self._execution, "_running", False))
        return snap

    # ── OI-bias history ────────────────────────────────────────────────
    OI_BIAS_UNDERLYINGS = ("NIFTY", "SENSEX")

    def ensure_oi_bias_recorder(self) -> None:
        """Start the per-minute OI-bias recorder (idempotent; needs a running loop)."""
        if self._oi_bias_task is None or self._oi_bias_task.done():
            self._oi_bias_task = asyncio.create_task(self._oi_bias_loop(), name="oi_bias_recorder")
            self._oi_bias_task.add_done_callback(self._task_exception_handler)

    async def record_oi_bias(self, underlying: str) -> dict[str, Any]:
        """Analyse ``underlying``'s near-ATM option chain once and store the bias row."""
        report = await asyncio.wait_for(self._oi_bias_analyzer.analyze(underlying, self._client), timeout=45)
        if not report.spot_price or not report.strikes:
            raise RuntimeError(f"no {underlying} option-chain data")
        sentiments = [s.get("sentiment") for s in report.buildup_signals or []]
        row = {
            "underlying": underlying,
            "ts": datetime.now().replace(second=0, microsecond=0).isoformat(),
            "spot_price": report.spot_price, "atm_strike": report.atm_strike, "expiry": report.expiry,
            "bias": report.bias, "pcr_oi": report.pcr_oi, "pcr_volume": report.pcr_volume,
            "total_ce_oi": report.total_ce_oi, "total_pe_oi": report.total_pe_oi, "max_pain": report.max_pain,
            "max_ce_oi_strike": report.max_ce_oi_strike, "max_pe_oi_strike": report.max_pe_oi_strike,
            "iv_skew": report.iv_skew, "straddle_premium": report.atm_straddle_premium,
            "bullish_signals": sentiments.count("BULLISH"), "bearish_signals": sentiments.count("BEARISH"),
            "reasons": report.bias_reasons,
        }
        self._market_store.store_oi_bias(row)
        previous = self.last_oi_bias.get(underlying, {}).get("bias")
        self.last_oi_bias[underlying] = row
        self._emit("oi.bias", **row, previous=previous)
        return row

    async def _oi_bias_loop(self) -> None:
        """While the market is open: one bias reading per underlying per minute."""
        from src.agent.market_hours import is_market_open

        while True:
            if self.is_authenticated and is_market_open():
                for underlying in self.OI_BIAS_UNDERLYINGS:
                    try:
                        await self.record_oi_bias(underlying)
                    except Exception as e:
                        logger.warning("oi_bias_record_failed", underlying=underlying, error=str(e))
                        self._emit("oi.bias_error", underlying=underlying, error=str(e))
                await asyncio.sleep(60 - datetime.now().second + 1)   # next minute boundary
            else:
                await asyncio.sleep(30)

    def get_oi_bias_history(self, underlying: str = "NIFTY", days: int = 1) -> dict[str, Any]:
        since = (datetime.now() - timedelta(days=max(1, min(days, 120)))).isoformat()
        rows = self._market_store.get_oi_bias(underlying.upper(), since)
        return {"underlying": underlying.upper(), "count": len(rows), "rows": rows}

    # ── board auto-trade ───────────────────────────────────────────────

    def get_board_autotrade(self) -> dict[str, Any]:
        return {**self._board.settings, "orders_today": self._board.orders_today(),
                "live_trading": self._running and bool(getattr(self._execution, "_running", False))}

    def update_board_autotrade(self, patch: dict[str, Any]) -> dict[str, Any]:
        settings = self._board.update_settings(patch, datetime.now())
        if settings["enabled"]:
            self._ensure_day_trade_loop()
        return self.get_board_autotrade()

    async def _send_board_signal(self, s: dict[str, Any], quantity: int, board: Any) -> dict[str, Any]:
        """Send one board signal as a real order through the live-trading path.

        Same order as the live ``day_trade_setups`` strategy would send
        (``build_order_signal``), the Verifier's preflight checklist run on that
        actual order, then ``_process_signal``: signal log, position rules, risk
        manager, kill switch, execution engine with the exit plan, journal.
        """
        from src.risk.preflight import PreflightChecker, TradingMode
        from src.strategy.day_trade_setups import build_order_signal

        now = datetime.now()
        if not (self._running and getattr(self._execution, "_running", False)):
            return {"decision": "blocked", "reason": "live trading is off — start live trading to auto-trade the board"}
        if not market_open(now):
            return {"decision": "blocked", "reason": "market is closed"}
        for name in ("day_trade_board", "day_trade_setups"):
            if self._execution.open_position_for(s["symbol"], name) is not None:
                return {"decision": "blocked", "reason": f"a {name} position in {s['symbol']} is already open"}
        signal = build_order_signal(
            s, symbol=s["symbol"], atr=float(s["atr"]), last_close=float(s["bar_close"]),
            quantity=quantity, strategy_name="day_trade_board",
        )
        signal.metadata = {**signal.metadata, "timeframe": "5minute", "source": "day_trade_board"}
        if board.settings.get("require_preflight", True):
            if self._board_preflight is None:
                self._board_preflight = PreflightChecker(client=self._client, ticker=self._ticker, risk_manager=self._risk)
            report = await asyncio.wait_for(self._board_preflight.run_all_checks(signal=signal, mode=TradingMode.LIVE),
                                            timeout=20)
            if not report.passed:
                return {"decision": "blocked", "reason": "preflight: " + "; ".join(report.blocked_reasons)}
        order_id = await self._process_signal(signal)
        if order_id:
            return {"decision": "sent", "order_id": order_id}
        return {"decision": "failed", "reason": "not placed by the live path (risk limit, kill switch or broker error — see the Executor log)"}

    async def replay_day_trade_setups(self, symbols: list[str], days: int = 30) -> dict[str, Any]:
        """Run the 5-minute setups over the last ``days`` sessions for each symbol.

        Same engine as the live panel and the ``day_trade_setups`` strategy;
        each session sees only earlier sessions. Results are net of costs.
        """
        from src.analysis.intraday_setups import session_stats
        from src.strategy.day_trade_setups import HISTORY_CALENDAR_DAYS, group_stats, replay_sessions

        days = max(5, min(int(days), 90))
        per_symbol: dict[str, Any] = {}
        combined: list[dict[str, Any]] = []
        for sym in [s.upper().strip() for s in symbols if s.strip()][:10]:
            token = await self.resolve_instrument_token(sym, "NSE")
            if not token:
                per_symbol[sym] = {"error": "symbol not found on NSE"}
                continue
            try:
                # extra history so the first replayed session has its full ATR window
                rows = await self._resolve_ohlcv_rows(token, "5minute", days=days + HISTORY_CALENDAR_DAYS)
            except Exception as e:
                per_symbol[sym] = {"error": f"history fetch failed: {e}"}
                continue
            df = pd.DataFrame(rows).rename(columns={"ts": "timestamp"})
            result = replay_sessions(df)
            for sig in result["signals"]:
                sig["symbol"] = sym
            combined.extend(result["signals"])
            per_symbol[sym] = {k: result[k] for k in ("sessions", "stats", "by_setup", "by_side")}
        combined.sort(key=lambda s: str(s["bar_time"]), reverse=True)
        return {
            "days": days,
            "symbols": per_symbol,
            "stats": session_stats(combined),
            "by_setup": group_stats(combined, "setup"),
            "by_side": group_stats(combined, "side"),
            "signals": combined[:300],
        }

    async def track_day_trade_symbol(self, symbol: str, action: str = "add") -> dict[str, Any]:
        symbol = symbol.upper().strip()
        if action == "remove":
            self._day_trade.remove(symbol)
            return {"success": True, "tracked": self._day_trade.symbols}
        flow = await self.get_fno_flow(max_age_s=5.0)
        if flow.get("error"):
            return {"error": flow["error"]}
        row = next((r for r in flow.get("rows", []) if r["symbol"] == symbol), None)
        if row is None:
            return {"error": f"{symbol} is not in the F&O or NIFTY 500 universe"}
        try:
            self._day_trade.add(symbol, datetime.now(), row)
        except ValueError as e:
            return {"error": str(e)}
        self._ensure_day_trade_loop()
        return {"success": True, "tracked": self._day_trade.symbols}

    async def get_nifty_contribution(self, max_age_s: float = 1.5) -> dict[str, Any]:
        """Per-stock Nifty 50 point contribution, cached briefly so concurrent
        pollers share one batched quote call."""
        async with self._nifty_contrib_lock:
            cached = self._nifty_contrib_cache
            if cached and time.monotonic() - cached[0] < max_age_s:
                return cached[1]
            resp = await self._market_data.get_quote(nifty_quote_instruments())
            if "error" in resp:
                return {"error": resp["error"]}
            result = compute_nifty_contribution(resp.get("quotes", {}))
            result["timestamp"] = datetime.now().isoformat(timespec="seconds")
            self._nifty_contrib_cache = (time.monotonic(), result)
            return result

    # ────────────────────────────────────────────────────────────
    # Portfolio Reports
    # ────────────────────────────────────────────────────────────

    async def get_holdings_report(self) -> dict[str, Any]:
        return await self._portfolio_reporter.get_holdings_report()

    async def get_positions_report(self) -> dict[str, Any]:
        return await self._portfolio_reporter.get_positions_report()

    async def get_trades_report(self) -> dict[str, Any]:
        return await self._portfolio_reporter.get_trades_report()

    async def get_orders_report(self) -> dict[str, Any]:
        return await self._portfolio_reporter.get_orders_report()

    async def get_pnl_report(self) -> dict[str, Any]:
        return await self._portfolio_reporter.get_pnl_report()

    async def get_margins_report(self) -> dict[str, Any]:
        return await self._portfolio_reporter.get_margins_report()

    # ────────────────────────────────────────────────────────────
    # Advanced Backtesting
    # ────────────────────────────────────────────────────────────

    def _journal_record_backtest_trades(
        self, result: dict[str, Any], strategy_name: str, tradingsymbol: str
    ) -> None:
        """Record backtest round-trip trades into JournalStore for analytics."""
        trades = result.get("trades", [])
        if not trades:
            return

        from src.journal.journal_models import JournalEntry, ExecutionRecord, TradeDirection

        # Pair entries with exits
        pending_entry: dict[str, Any] | None = None
        recorded = 0

        for t in trades:
            ttype = t.get("type", "")
            if ttype.endswith("_ENTRY"):
                pending_entry = t
            elif ("EXIT" in ttype) and pending_entry:
                # Round-trip complete
                direction = TradeDirection.LONG if "LONG" in pending_entry.get("type", "") else TradeDirection.SHORT
                entry_price = pending_entry.get("entry_price", 0)
                exit_price = t.get("exit_price", 0)
                qty = pending_entry.get("quantity", 1)
                pnl = t.get("pnl", 0)
                costs = t.get("costs", 0)
                
                exec_rec = ExecutionRecord(
                    trade_id=f"BT-{strategy_name}-{recorded}",
                    instrument=tradingsymbol,
                    exchange="NSE",
                    direction=direction.value if hasattr(direction, 'value') else str(direction),
                    quantity=qty,
                    actual_fill_price=entry_price,
                    actual_exit_price=exit_price,
                    expected_entry_price=entry_price,
                    net_pnl=pnl,
                    gross_pnl=pnl + costs,
                )
                entry = JournalEntry(
                    trade_id=f"BT-{strategy_name}-{recorded}",
                    instrument=tradingsymbol,
                    tradingsymbol=tradingsymbol,
                    exchange="NSE",
                    direction=direction.value if hasattr(direction, 'value') else str(direction),
                    is_closed=True,
                    net_pnl=pnl,
                    gross_pnl=pnl + costs,
                    total_costs=costs,
                    strategy_name=strategy_name,
                    source="backtest",
                    entry_time=pending_entry.get("entry_time", ""),
                    exit_time=t.get("exit_time", ""),
                    mae=t.get("mae_pct", 0),
                    mfe=t.get("mfe_pct", 0),
                    execution=exec_rec.to_dict(),
                    tags=["backtest", strategy_name, ttype.replace("_EXIT", "").replace("SL_", "sl_")],
                    notes=f"Backtest {strategy_name} on {tradingsymbol} | Exit: {ttype}",
                )
                try:
                    self._journal_store.record_entry(entry)
                    recorded += 1
                except Exception as exc:
                    logger.error("backtest_journal_entry_error", error=str(exc), trade=recorded)
                pending_entry = None

        if recorded > 0:
            logger.info("backtest_trades_journaled", count=recorded, strategy=strategy_name)

    def _journal_record_paper_trades(
        self, result: dict[str, Any], strategy_name: str, tradingsymbol: str
    ) -> None:
        """Record paper trade positions into JournalStore for analytics."""
        positions = result.get("positions", [])
        if not positions:
            return

        from src.journal.journal_models import JournalEntry, ExecutionRecord, TradeDirection

        recorded = 0
        for i, pos in enumerate(positions):
            direction = TradeDirection.LONG if pos.get("type") == "LONG" else TradeDirection.SHORT
            entry_price = pos.get("entry", 0)
            exit_price = pos.get("exit", 0)
            qty = pos.get("qty", 1)
            pnl = pos.get("pnl", 0)
            reason = pos.get("reason", "")

            exec_rec = ExecutionRecord(
                trade_id=f"PT-{strategy_name}-{i}",
                instrument=tradingsymbol,
                exchange="NSE",
                direction=direction.value if hasattr(direction, 'value') else str(direction),
                quantity=qty,
                actual_fill_price=entry_price,
                actual_exit_price=exit_price,
                expected_entry_price=entry_price,
                net_pnl=pnl,
                gross_pnl=pnl,
            )
            entry = JournalEntry(
                trade_id=f"PT-{strategy_name}-{i}",
                instrument=tradingsymbol,
                tradingsymbol=tradingsymbol,
                exchange="NSE",
                direction=direction.value if hasattr(direction, 'value') else str(direction),
                is_closed=True,
                net_pnl=pnl,
                gross_pnl=pnl,
                strategy_name=strategy_name,
                source="paper_trade",
                entry_time=pos.get("entry_time", ""),
                exit_time=pos.get("exit_time", ""),
                execution=exec_rec.to_dict(),
                tags=["paper_trade", strategy_name, reason or "signal"],
                notes=f"Paper trade {strategy_name} on {tradingsymbol} | Exit: {reason}",
            )
            try:
                self._journal_store.record_entry(entry)
                recorded += 1
            except Exception as exc:
                logger.error("paper_journal_entry_error", error=str(exc), trade=i)

        if recorded > 0:
            logger.info("paper_trades_journaled", count=recorded, strategy=strategy_name)

    def _journal_record_fno_backtest_trades(
        self, result: dict[str, Any], strategy_name: str, underlying: str
    ) -> None:
        """Record F&O backtest trades into JournalStore.

        F&O trades have a different format from equity:
          ENTRY → {type: ENTRY, structure, legs, net_premium, costs, position_id, ...}
          EXIT  → {type: PROFIT_TARGET|STOP_LOSS|FINAL_EXIT|EXPIRY, pnl, costs, position_id, ...}
        We pair ENTRY with its corresponding EXIT by position_id for a round-trip.
        """
        trades = result.get("trades", [])
        if not trades:
            return

        from src.journal.journal_models import JournalEntry, ExecutionRecord

        # Group trades by position_id: pair entries with exits
        entries_by_pos: dict[str, dict] = {}
        exits_by_pos: dict[str, dict] = {}
        for t in trades:
            pid = t.get("position_id", "")
            if not pid:
                continue
            ttype = t.get("type", "")
            if ttype == "ENTRY":
                entries_by_pos[pid] = t
            elif ttype in ("PROFIT_TARGET", "STOP_LOSS", "FINAL_EXIT", "EXPIRY"):
                exits_by_pos[pid] = t

        recorded = 0
        structure = result.get("structure", strategy_name)
        for pid, entry_t in entries_by_pos.items():
            exit_t = exits_by_pos.get(pid)
            if not exit_t:
                continue  # position not closed yet — skip

            pnl = exit_t.get("pnl", 0)
            costs = entry_t.get("costs", 0) + exit_t.get("costs", 0)
            net_premium = entry_t.get("net_premium", 0)
            margin = entry_t.get("margin", 0)
            exit_type = exit_t.get("type", "")

            # Determine direction from net_premium: credit strategies are SHORT, debit are LONG
            direction = "SHORT" if net_premium > 0 else "LONG"

            exec_rec = ExecutionRecord(
                trade_id=f"FNO-BT-{strategy_name}-{recorded}",
                instrument=underlying,
                exchange="NFO",
                direction=direction,
                quantity=entry_t.get("legs", 1),
                actual_fill_price=abs(net_premium),
                actual_exit_price=0,
                expected_entry_price=abs(net_premium),
                net_pnl=pnl,
                gross_pnl=pnl + costs,
            )
            entry = JournalEntry(
                trade_id=f"FNO-BT-{strategy_name}-{recorded}",
                instrument=underlying,
                tradingsymbol=underlying,
                exchange="NFO",
                direction=direction,
                trade_type="fno",
                is_closed=True,
                net_pnl=pnl,
                gross_pnl=pnl + costs,
                total_costs=costs,
                strategy_name=f"fno_{strategy_name}",
                source="backtest",
                entry_time=entry_t.get("date", ""),
                exit_time=exit_t.get("date", ""),
                execution=exec_rec.to_dict(),
                position_structure={
                    "structure": structure,
                    "legs": entry_t.get("legs", 0),
                    "net_premium": net_premium,
                    "initial_margin": margin,
                    "spot_at_entry": entry_t.get("spot", 0),
                },
                return_on_margin=round(pnl / margin * 100, 2) if margin > 0 else 0,
                tags=["backtest", "fno", strategy_name, structure, exit_type.lower()],
                notes=f"F&O Backtest {structure} on {underlying} | {exit_type} | PnL: {pnl:.2f}",
            )
            try:
                self._journal_store.record_entry(entry)
                recorded += 1
            except Exception as exc:
                logger.error("fno_backtest_journal_entry_error", error=str(exc), trade=recorded)

        if recorded > 0:
            logger.info("fno_backtest_trades_journaled", count=recorded, strategy=strategy_name)

    def _journal_record_fno_paper_trades(
        self, result: dict[str, Any], strategy_name: str, underlying: str
    ) -> None:
        """Record F&O paper trade positions into JournalStore.

        F&O paper trade positions have format:
          {id, structure, legs, net_premium, pnl, entry, exit, regime}
        """
        positions = result.get("positions", [])
        if not positions:
            return

        from src.journal.journal_models import JournalEntry, ExecutionRecord

        recorded = 0
        for i, pos in enumerate(positions):
            pnl = pos.get("pnl", 0)
            net_premium = pos.get("net_premium", 0)
            structure = pos.get("structure", strategy_name)
            direction = "SHORT" if net_premium > 0 else "LONG"

            exec_rec = ExecutionRecord(
                trade_id=f"FNO-PT-{strategy_name}-{i}",
                instrument=underlying,
                exchange="NFO",
                direction=direction,
                quantity=pos.get("legs", 1),
                actual_fill_price=abs(net_premium),
                actual_exit_price=0,
                expected_entry_price=abs(net_premium),
                net_pnl=pnl,
                gross_pnl=pnl,
            )
            entry = JournalEntry(
                trade_id=f"FNO-PT-{strategy_name}-{i}",
                instrument=underlying,
                tradingsymbol=underlying,
                exchange="NFO",
                direction=direction,
                trade_type="fno",
                is_closed=True,
                net_pnl=pnl,
                gross_pnl=pnl,
                strategy_name=f"fno_{strategy_name}",
                source="paper_trade",
                entry_time=pos.get("entry", ""),
                exit_time=pos.get("exit", ""),
                execution=exec_rec.to_dict(),
                position_structure={
                    "structure": structure,
                    "legs": pos.get("legs", 0),
                    "net_premium": net_premium,
                    "regime_at_entry": pos.get("regime", ""),
                },
                tags=["paper_trade", "fno", strategy_name, structure],
                notes=f"F&O Paper {structure} on {underlying} | PnL: {pnl:.2f}",
            )
            try:
                self._journal_store.record_entry(entry)
                recorded += 1
            except Exception as exc:
                logger.error("fno_paper_journal_entry_error", error=str(exc), trade=i)

        if recorded > 0:
            logger.info("fno_paper_trades_journaled", count=recorded, strategy=strategy_name)

    async def run_backtest(
        self,
        strategy_name: str,
        instrument_token: int = 0,
        tradingsymbol: str = "BACKTEST",
        exchange: str = "NSE",
        interval: str = "day",
        days: int = 365,
        from_date: str | None = None,
        to_date: str | None = None,
        initial_capital: float = 100_000.0,
        position_sizing: str = "fixed",
        risk_per_trade: float = 0.02,
        capital_fraction: float = 0.10,
        slippage_pct: float = 0.05,
        use_indian_costs: bool = True,
        is_intraday: bool = True,
        strategy_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run advanced backtest on historical Zerodha data."""
        strategy = self._resolve_strategy(strategy_name, strategy_params)
        if isinstance(strategy, dict):
            return strategy  # error dict
        # strategies built for one bar size (e.g. 5-minute day-trade setups) run on it
        interval = getattr(strategy, "required_interval", None) or interval

        # Extend fetch with warm-up history so the 200-DMA regime filter is
        # active across the whole requested window (see _with_dma200_warmup).
        fetch_days = self._with_dma200_warmup(strategy, interval, days, from_date)

        # Fetch historical data (DB cache first, live Kite fetch as fallback)
        rows = await self._resolve_ohlcv_rows(instrument_token, interval, fetch_days, from_date, to_date)
        if not rows:
            return {"error": "No historical data available. Check instrument token and date range."}
        df = pd.DataFrame(rows).rename(columns={"ts": "timestamp"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed")
        df.set_index("timestamp", inplace=True)

        engine = BacktestEngine(
            strategy=strategy,
            initial_capital=initial_capital,
            slippage_pct=slippage_pct,
            position_sizing=position_sizing,
            risk_per_trade=risk_per_trade,
            capital_fraction=capital_fraction,
            use_indian_costs=use_indian_costs,
            is_intraday=is_intraday,
        )

        result = await asyncio.to_thread(engine.run, df, instrument_token, tradingsymbol)
        result["generated_at"] = datetime.now().isoformat()
        if fetch_days != days:
            result["warmup_note"] = (
                f"Fetched {fetch_days}d (requested {days}d + warm-up) so the 200-DMA "
                "regime filter is active across the whole requested window; no trades "
                "occur in the warm-up stretch at the start."
            )
        self._backtest_results = result
        # Stash symbol/exchange/token so an execution-ready backtest can be
        # promoted to a live strategy and auto-subscribed without re-entry.
        self._backtest_results["tradingsymbol"] = tradingsymbol
        self._backtest_results["exchange"] = exchange
        self._backtest_results["instrument_token"] = instrument_token
        # Capture the validated timeframe so promote can seed the SAME interval
        # the strategy was backtested on (fixes live-on-5min vs backtest-on-X
        # mismatch).
        self._backtest_results["interval"] = interval

        # Compute strategy health report and embed in result
        try:
            stype = "intraday" if is_intraday else "equity"
            health = self.compute_strategy_health(result, strategy_type=stype, strategy_name=strategy_name)
            result["health_report"] = health
        except Exception as e:
            logger.error("health_compute_error", error=str(e))

        # Record backtest trades to journal for analysis
        try:
            self._journal_record_backtest_trades(result, strategy_name, tradingsymbol)
        except Exception as e:
            logger.error("backtest_journal_record_error", error=str(e))

        return result

    async def run_backtest_sample(
        self,
        strategy_name: str,
        bars: int = 500,
        initial_capital: float = 100_000.0,
        position_sizing: str = "fixed",
        slippage_pct: float = 0.05,
        use_indian_costs: bool = True,
        is_intraday: bool = False,
        risk_per_trade: float = 0.02,
        capital_fraction: float = 0.10,
        strategy_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run backtest on synthetic sample data."""
        strategy = self._resolve_strategy(strategy_name, strategy_params)
        if isinstance(strategy, dict):
            return strategy

        import numpy as np
        np.random.seed(42)
        dates = pd.date_range(end=pd.Timestamp.now(), periods=bars, freq="D")
        base = 100.0

        # Generate realistic price data with trend regimes and mean-reversion
        # Instead of pure random walk, create alternating trending/ranging periods
        prices = np.zeros(bars)
        prices[0] = base
        regime_length = max(20, bars // 10)  # ~10 regime changes
        trend = 0.0
        volatility = 0.02
        for i in range(1, bars):
            # Switch regime every regime_length bars
            if i % regime_length == 0:
                trend = np.random.choice([-0.003, -0.001, 0.0, 0.001, 0.003])
                volatility = np.random.choice([0.015, 0.02, 0.03])
            noise = np.random.normal(trend, volatility)
            prices[i] = prices[i - 1] * (1 + noise)

        # Volume with spikes: base volume + occasional spikes for breakout signals
        base_vol = np.random.randint(500_000, 2_000_000, bars).astype(float)
        # Add volume spikes (~20% of bars get 1.5-3x volume)
        spike_mask = np.random.random(bars) < 0.20
        base_vol[spike_mask] *= np.random.uniform(1.5, 3.0, spike_mask.sum())
        volumes = base_vol.astype(int)

        df = pd.DataFrame({
            "timestamp": dates,
            "open": prices * (1 + np.random.uniform(-0.008, 0.008, bars)),
            "high": prices * (1 + np.abs(np.random.normal(0, 0.012, bars))),
            "low": prices * (1 - np.abs(np.random.normal(0, 0.012, bars))),
            "close": prices,
            "volume": volumes,
        }, index=dates)

        engine = BacktestEngine(
            strategy=strategy, initial_capital=initial_capital,
            slippage_pct=slippage_pct, position_sizing=position_sizing,
            risk_per_trade=risk_per_trade, capital_fraction=capital_fraction,
            use_indian_costs=use_indian_costs, is_intraday=is_intraday,
        )
        result = await asyncio.to_thread(engine.run, df, tradingsymbol="SAMPLE")
        result["generated_at"] = datetime.now().isoformat()
        self._backtest_results = result

        # Compute strategy health report and embed in result
        try:
            stype = "intraday" if is_intraday else "equity"
            health = self.compute_strategy_health(result, strategy_type=stype, strategy_name=strategy_name)
            result["health_report"] = health
        except Exception as e:
            logger.error("health_compute_error", error=str(e))

        # Record backtest trades to journal for analysis
        try:
            self._journal_record_backtest_trades(result, strategy_name, "SAMPLE")
        except Exception as e:
            logger.error("backtest_sample_journal_record_error", error=str(e))

        return result

    def get_backtest_results(self) -> dict[str, Any]:
        return self._backtest_results.copy() if self._backtest_results else {"error": "No backtest results"}

    def compute_strategy_health(
        self,
        result: Optional[dict[str, Any]] = None,
        strategy_type: str = "",
        strategy_name: str = "",
    ) -> dict[str, Any]:
        """Compute strategy health report from backtest/paper-trade results.
        
        If result is None, uses the last cached backtest result.
        """
        if result is None:
            result = self._backtest_results
        if not result or "error" in result:
            return {"error": "No backtest/paper-trade results available. Run a backtest first."}

        # Auto-detect strategy type
        if not strategy_type:
            engine = result.get("engine", "")
            if "fno" in engine.lower():
                strategy_type = "fno"
            else:
                strategy_type = "equity"

        if not strategy_name:
            strategy_name = result.get("strategy_name", "unknown")

        report = compute_health_report(result, strategy_type=strategy_type, strategy_name=strategy_name)
        report_dict = sanitize_for_json(report.to_dict())
        report_dict["grade"] = get_health_grade(report.overall_score)

        # Cache
        self._last_health_report = report_dict

        # Record health score in journal metadata
        try:
            self._record_health_to_journal(report, result)
        except Exception as e:
            logger.error("health_journal_record_error", error=str(e))

        return report_dict

    def get_last_health_report(self) -> dict[str, Any]:
        """Return the last computed health report."""
        if self._last_health_report:
            return self._last_health_report
        return {"error": "No health report computed yet. Run a backtest first."}

    def _record_health_to_journal(self, report: HealthReport, result: dict) -> None:
        """Store strategy health score as a journal metadata entry."""
        try:
            strategy_name = report.strategy_name
            entry = JournalEntry(
                entry_id=f"health-{strategy_name}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                trade_type=report.strategy_type or "equity",
                strategy_name=strategy_name,
                instrument=result.get("underlying", result.get("tradingsymbol", "")),
                source="health_report",
                notes=f"Health Score: {report.overall_score:.1f} ({report.overall_verdict})",
                tags=["health_report", report.overall_verdict.lower(), strategy_name],
                metadata={
                    "health_score": round(report.overall_score, 1),
                    "health_grade": get_health_grade(report.overall_score),
                    "health_verdict": report.overall_verdict,
                    "execution_ready": report.execution_ready,
                    "pillar_scores": {k: round(v.score, 1) for k, v in report.pillars.items()},
                    "pillar_verdicts": {k: v.verdict for k, v in report.pillars.items()},
                    "blockers": report.blockers,
                    "warnings": report.warnings,
                    "strategy_type": report.strategy_type,
                    "total_trades": result.get("total_trades", 0),
                    "expectancy": report.pillars.get("profitability", PillarResult(name="")).metrics.get("expectancy", 0),
                    "profit_factor": report.pillars.get("profitability", PillarResult(name="")).metrics.get("profit_factor", 0),
                    "sharpe_ratio": report.pillars.get("profitability", PillarResult(name="")).metrics.get("sharpe_ratio", 0),
                    "max_drawdown_pct": report.pillars.get("drawdown", PillarResult(name="")).metrics.get("max_drawdown_pct", 0),
                },
            )
            self._journal_store.record_entry(entry)
        except Exception as e:
            logger.error("health_journal_error", error=str(e))


    # ────────────────────────────────────────────────────────────
    # F&O Derivatives Backtesting & Paper Trading
    # ────────────────────────────────────────────────────────────

    FNO_STRATEGIES = [
        "iron_condor", "bull_call_spread", "bear_put_spread",
        "bull_put_spread", "bear_call_spread", "straddle",
        "short_straddle", "strangle", "short_strangle",
        "iron_butterfly", "covered_call", "protective_put",
        "calendar_spread",
    ]

    def _is_valid_fno_strategy(self, name: str) -> bool:
        """Check if strategy name is a built-in or saved custom F&O strategy."""
        if name.lower() in self.FNO_STRATEGIES:
            return True
        # Also accept saved custom strategies from the builder
        return self.get_fno_custom_strategy(name) is not None

    async def run_fno_backtest(
        self,
        strategy_name: str = "iron_condor",
        underlying: str = "NIFTY",
        instrument_token: int = 0,
        interval: str = "day",
        days: int = 365,
        from_date: str | None = None,
        to_date: str | None = None,
        initial_capital: float = 500_000.0,
        max_positions: int = 3,
        profit_target_pct: float = 50.0,
        stop_loss_pct: float = 100.0,
        entry_dte_min: int = 15,
        entry_dte_max: int = 45,
        delta_target: float = 0.16,
        slippage_model: str = "realistic",
        use_regime_filter: bool = True,
    ) -> dict[str, Any]:
        """Run F&O derivatives backtest on underlying OHLCV data."""
        if not self._is_valid_fno_strategy(strategy_name):
            return {
                "error": f"Unknown F&O strategy: {strategy_name}",
                "available": self.FNO_STRATEGIES,
            }

        # Fetch underlying data (DB cache first, live Kite fetch as fallback)
        token = self.resolve_underlying_token(underlying, instrument_token)
        if not token:
            return {"error": f"Unknown underlying: {underlying}. Provide instrument_token explicitly."}
        rows = await self._resolve_ohlcv_rows(token, interval, days, from_date, to_date)
        if not rows:
            return {"error": "No historical data available. Check underlying and date range."}
        df = pd.DataFrame(rows).rename(columns={"ts": "timestamp"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed")
        df.set_index("timestamp", inplace=True)

        engine = FnOBacktestEngine(
            strategy_name=strategy_name,
            underlying=underlying,
            initial_capital=initial_capital,
            max_positions=max_positions,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            entry_dte_min=entry_dte_min,
            entry_dte_max=entry_dte_max,
            delta_target=delta_target,
            slippage_model=slippage_model,
            use_regime_filter=use_regime_filter,
        )

        result = await asyncio.to_thread(engine.run, df, tradingsymbol=underlying)
        result["generated_at"] = datetime.now().isoformat()
        self._backtest_results = result

        # Compute strategy health report and embed in result
        try:
            health = self.compute_strategy_health(result, strategy_type="fno", strategy_name=strategy_name)
            result["health_report"] = health
        except Exception as e:
            logger.error("health_compute_error", error=str(e))

        # Record F&O backtest trades to journal
        try:
            self._journal_record_fno_backtest_trades(result, strategy_name, underlying)
        except Exception as e:
            logger.error("fno_backtest_journal_record_error", error=str(e))

        return result

    async def run_fno_backtest_sample(
        self,
        strategy_name: str = "iron_condor",
        underlying: str = "NIFTY",
        bars: int = 500,
        initial_capital: float = 500_000.0,
        max_positions: int = 3,
        profit_target_pct: float = 50.0,
        stop_loss_pct: float = 100.0,
        delta_target: float = 0.16,
    ) -> dict[str, Any]:
        """Run F&O backtest on synthetic data (no auth required)."""
        if not self._is_valid_fno_strategy(strategy_name):
            return {
                "error": f"Unknown F&O strategy: {strategy_name}",
                "available": self.FNO_STRATEGIES,
            }

        import numpy as np
        np.random.seed(42)
        # Simulate index-like data (NIFTY ~20000)
        base = 20000.0
        dates = pd.date_range(end=pd.Timestamp.now(), periods=bars, freq="D")
        returns = np.random.normal(0.0003, 0.012, bars)
        prices = base * np.cumprod(1 + returns)
        df = pd.DataFrame({
            "open": prices * (1 + np.random.uniform(-0.003, 0.003, bars)),
            "high": prices * (1 + np.abs(np.random.normal(0, 0.008, bars))),
            "low": prices * (1 - np.abs(np.random.normal(0, 0.008, bars))),
            "close": prices,
            "volume": np.random.randint(100000, 5000000, bars),
        }, index=dates)

        engine = FnOBacktestEngine(
            strategy_name=strategy_name,
            underlying=underlying,
            initial_capital=initial_capital,
            max_positions=max_positions,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            delta_target=delta_target,
        )
        result = await asyncio.to_thread(engine.run, df, tradingsymbol=underlying)
        result["generated_at"] = datetime.now().isoformat()
        self._backtest_results = result

        # Compute strategy health report and embed in result
        try:
            health = self.compute_strategy_health(result, strategy_type="fno", strategy_name=strategy_name)
            result["health_report"] = health
        except Exception as e:
            logger.error("health_compute_error", error=str(e))

        # Record F&O backtest sample trades to journal
        try:
            self._journal_record_fno_backtest_trades(result, strategy_name, underlying)
        except Exception as e:
            logger.error("fno_backtest_sample_journal_record_error", error=str(e))

        return result

    async def run_fno_paper_trade(
        self,
        strategy_name: str = "iron_condor",
        underlying: str = "NIFTY",
        instrument_token: int = 0,
        interval: str = "day",
        days: int = 60,
        initial_capital: float = 500_000.0,
        max_positions: int = 3,
        profit_target_pct: float = 50.0,
        stop_loss_pct: float = 100.0,
        entry_dte_min: int = 15,
        entry_dte_max: int = 45,
        delta_target: float = 0.16,
        slippage_model: str = "realistic",
    ) -> dict[str, Any]:
        """Run F&O paper trading simulation."""
        if not self._is_valid_fno_strategy(strategy_name):
            return {
                "error": f"Unknown F&O strategy: {strategy_name}",
                "available": self.FNO_STRATEGIES,
            }

        token = self.resolve_underlying_token(underlying, instrument_token)
        if not token:
            return {"error": f"Unknown underlying: {underlying}. Provide instrument_token explicitly."}
        rows = await self._resolve_ohlcv_rows(token, interval, days)
        if not rows:
            return {"error": "No historical data available. Check underlying and date range."}
        df = pd.DataFrame(rows).rename(columns={"ts": "timestamp"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed")
        df.set_index("timestamp", inplace=True)

        engine = FnOPaperTradingEngine(
            strategy_name=strategy_name,
            underlying=underlying,
            initial_capital=initial_capital,
            max_positions=max_positions,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            entry_dte_min=entry_dte_min,
            entry_dte_max=entry_dte_max,
            delta_target=delta_target,
            slippage_model=slippage_model,
        )
        result = await asyncio.to_thread(engine.run, df, tradingsymbol=underlying, timeframe=interval)
        result_dict = result.to_dict_safe()
        result_dict["generated_at"] = datetime.now().isoformat()
        self._paper_results[f"fno_{strategy_name}"] = result_dict

        # Compute strategy health report and embed in result
        try:
            health = self.compute_strategy_health(result_dict, strategy_type="fno", strategy_name=strategy_name)
            result_dict["health_report"] = health
        except Exception as e:
            logger.error("health_compute_error", error=str(e))

        # Record F&O paper trades to journal
        try:
            self._journal_record_fno_paper_trades(result_dict, strategy_name, underlying)
        except Exception as e:
            logger.error("fno_paper_trade_journal_record_error", error=str(e))

        return result_dict

    async def run_fno_paper_trade_sample(
        self,
        strategy_name: str = "iron_condor",
        underlying: str = "NIFTY",
        bars: int = 500,
        initial_capital: float = 500_000.0,
        max_positions: int = 3,
        profit_target_pct: float = 50.0,
        stop_loss_pct: float = 100.0,
        delta_target: float = 0.16,
    ) -> dict[str, Any]:
        """Run F&O paper trade on synthetic data (no auth required)."""
        if not self._is_valid_fno_strategy(strategy_name):
            return {
                "error": f"Unknown F&O strategy: {strategy_name}",
                "available": self.FNO_STRATEGIES,
            }

        import numpy as np
        np.random.seed(42)
        base = 20000.0
        dates = pd.date_range(end=pd.Timestamp.now(), periods=bars, freq="D")
        returns = np.random.normal(0.0003, 0.012, bars)
        prices = base * np.cumprod(1 + returns)
        df = pd.DataFrame({
            "open": prices * (1 + np.random.uniform(-0.003, 0.003, bars)),
            "high": prices * (1 + np.abs(np.random.normal(0, 0.008, bars))),
            "low": prices * (1 - np.abs(np.random.normal(0, 0.008, bars))),
            "close": prices,
            "volume": np.random.randint(100000, 5000000, bars),
        }, index=dates)

        engine = FnOPaperTradingEngine(
            strategy_name=strategy_name,
            underlying=underlying,
            initial_capital=initial_capital,
            max_positions=max_positions,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            delta_target=delta_target,
        )
        result = await asyncio.to_thread(engine.run, df, tradingsymbol=underlying, timeframe="day")
        result_dict = result.to_dict_safe()
        result_dict["generated_at"] = datetime.now().isoformat()
        self._paper_results[f"fno_{strategy_name}"] = result_dict

        # Compute strategy health report and embed in result
        try:
            health = self.compute_strategy_health(result_dict, strategy_type="fno", strategy_name=strategy_name)
            result_dict["health_report"] = health
        except Exception as e:
            logger.error("health_compute_error", error=str(e))

        # Record F&O paper trade sample to journal
        try:
            self._journal_record_fno_paper_trades(result_dict, strategy_name, underlying)
        except Exception as e:
            logger.error("fno_paper_sample_journal_record_error", error=str(e))

        return result_dict

    def get_fno_strategies(self) -> list[str]:
        """Get available F&O strategy names."""
        return self.FNO_STRATEGIES

    async def _position_update_loop(self) -> None:
        while self._running:
            try:
                positions = await self._client.get_positions()
                self._risk.update_positions(positions.net)
                daily_pnl = sum(p.pnl for p in positions.net)
                self._risk.update_daily_pnl(daily_pnl)
                await self._broadcast_ws({
                    "type": "positions",
                    "data": positions.model_dump(),
                    "pnl": daily_pnl,
                })
            except Exception as e:
                logger.error("position_update_error", error=str(e))
            await asyncio.sleep(30)

    async def _oi_snapshot_loop(self) -> None:
        """Periodically persist the OI tracker's current snapshots for offline analysis."""
        while self._running:
            try:
                for underlying in ("NIFTY", "SENSEX"):
                    rows = self._oi_tracker.get_current_snapshots(underlying)
                    if rows:
                        self._market_store.store_oi_snapshots(rows)
            except Exception as e:
                logger.error("oi_snapshot_store_failed", error=str(e))
            await asyncio.sleep(30)

    # ────────────────────────────────────────────────────────────
    # Market Replay (paper-trade a strategy bar-by-bar over cached
    # historical data, streamed over the live websocket like a feed)
    # ────────────────────────────────────────────────────────────

    def get_replay_status(self) -> dict[str, Any]:
        return {k: v for k, v in self._replay_state.items() if k != "running"}

    async def start_replay(
        self,
        strategy_name: str,
        instrument_token: int,
        tradingsymbol: str,
        exchange: str = "NSE",
        interval: str = "5minute",
        days: int = 30,
        capital: float = 100000.0,
        speed: float = 10.0,
        strategy_params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Start replaying cached historical candles bar-by-bar as if they were live,
        driving a strategy + PaperTradingEngine in near-real-time (scaled by ``speed``).
        """
        if self._replay_state.get("running"):
            return {"error": "A replay is already running. Stop it first."}

        strategy = self._resolve_strategy(strategy_name, strategy_params)
        if isinstance(strategy, dict):  # error dict
            return strategy

        rows = await self._resolve_ohlcv_rows(instrument_token, interval, days=days)
        if not rows:
            return {"error": "No historical data available for replay. Import historical data first."}

        df = pd.DataFrame(rows).rename(columns={"ts": "timestamp"})
        engine = PaperTradingEngine(strategy=strategy, initial_capital=capital)

        self._replay_state = {
            "status": "starting",
            "running": True,
            "strategy": strategy_name,
            "tradingsymbol": tradingsymbol,
            "interval": interval,
            "total": len(df),
            "progress": 0,
        }
        self._replay_task = asyncio.create_task(
            self._replay_loop(df, engine, instrument_token, tradingsymbol, exchange, interval, speed)
        )
        self._replay_task.add_done_callback(self._task_exception_handler)
        return {"success": True, "total_bars": len(df)}

    def stop_replay(self) -> dict[str, Any]:
        if not self._replay_state.get("running"):
            return {"error": "No replay is running"}
        self._replay_state["running"] = False
        return {"success": True}

    async def _replay_loop(
        self,
        df: pd.DataFrame,
        engine: PaperTradingEngine,
        instrument_token: int,
        tradingsymbol: str,
        exchange: str,
        timeframe: str,
        speed: float,
    ) -> None:
        strategy = engine.strategy
        # Tell the strategy the replay interval so it scales lookbacks to it.
        if hasattr(strategy, "set_timeframe"):
            strategy.set_timeframe(timeframe)
        df = df.reset_index(drop=True)
        total = len(df)
        delay = max(0.0, 1.0 / max(speed, 0.01))
        self._replay_state["status"] = "running"

        try:
            for i in range(1, total):
                if not self._replay_state.get("running"):
                    self._replay_state["status"] = "stopped"
                    break

                bar = df.iloc[i]
                slice_df = df.iloc[: i + 1]
                strategy.update_bar_data(instrument_token, slice_df)
                signal = strategy.generate_signal(slice_df, instrument_token)

                price = float(bar["close"])
                bar_ts = pd.Timestamp(bar["timestamp"]).to_pydatetime() if "timestamp" in df.columns else datetime.now()

                if signal:
                    pos = engine._positions.get(tradingsymbol)
                    current_qty = pos.quantity if pos else 0
                    qty = max(1, int(getattr(signal, "quantity", 1) or 1))
                    if signal.transaction_type == TransactionType.BUY and current_qty <= 0:
                        engine.place_order(tradingsymbol, TransactionType.BUY, qty + abs(current_qty), price, timestamp=bar_ts)
                    elif signal.transaction_type == TransactionType.SELL and current_qty >= 0:
                        engine.place_order(tradingsymbol, TransactionType.SELL, qty + abs(current_qty), price, timestamp=bar_ts)

                engine.update_prices({tradingsymbol: price})
                self._replay_state["progress"] = i

                await self._broadcast_ws({
                    "type": "replay_tick",
                    "data": {
                        "timestamp": str(bar_ts),
                        "tradingsymbol": tradingsymbol,
                        "price": price,
                        "portfolio_value": engine.get_portfolio_value(),
                        "progress": i,
                        "total": total,
                    },
                })
                if delay > 0:
                    await asyncio.sleep(delay)

            if self._replay_state.get("status") == "running" and total:
                last_price = float(df.iloc[-1]["close"])
                last_ts = pd.Timestamp(df.iloc[-1]["timestamp"]).to_pydatetime() if "timestamp" in df.columns else datetime.now()
                engine.close_all_positions({tradingsymbol: last_price}, timestamp=last_ts)
                self._replay_state["status"] = "completed"

            report = engine.get_performance_report()
            result_dict: dict[str, Any] = {
                "strategy_name": getattr(strategy, "name", str(strategy)),
                "tradingsymbol": tradingsymbol,
                "final_capital": engine.get_portfolio_value(),
                "total_pnl": engine.get_pnl(),
                "equity_curve": engine.get_equity_curve(),
                **report,
            }
            self._replay_state["result"] = result_dict
            await self._broadcast_ws({"type": "replay_complete", "data": result_dict})
        except Exception as e:
            logger.error("replay_loop_error", error=str(e))
            self._replay_state["status"] = "error"
            self._replay_state["error"] = str(e)
            await self._broadcast_ws({"type": "replay_error", "data": {"error": str(e)}})
        finally:
            self._replay_state["running"] = False



class TradingServiceManager:
    """Backward-compatible facade over the MCP driver supervisor agent.

    Kept so existing call sites (``TradingServiceManager.get_instance()``) keep
    working unchanged; underneath, every TradingService is now created and
    supervised by an agent via ``MCPDriverSupervisorAgent``.
    """

    _instance: Optional[TradingServiceManager] = None

    def __init__(self) -> None:
        from src.agent.mcp_driver import MCPDriverSupervisorAgent  # local import avoids circular import

        self._mcp_driver = MCPDriverSupervisorAgent.get_instance()

    @classmethod
    def get_instance(cls) -> TradingServiceManager:
        if cls._instance is None:
            cls._instance = TradingServiceManager()
        return cls._instance

    def get_service(self, zerodha_id: str, api_key: str = "", api_secret: str = "") -> TradingService:
        return self._mcp_driver.get_service(zerodha_id, api_key=api_key, api_secret=api_secret)

    def has_service(self, zerodha_id: str) -> bool:
        return self._mcp_driver.has_service(zerodha_id)

    def remove_service(self, zerodha_id: str) -> None:
        self._mcp_driver.remove_service(zerodha_id)
