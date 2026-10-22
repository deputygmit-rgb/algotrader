"""ScannerAgent: autonomous, interval-driven market-wide idea scanner.

Wraps the existing ``TradingService.run_analysis_scan`` (backed by
``StockScanner`` and the tested technical strategies in ``src/strategy``)
with a self-scheduling loop, so trading ideas are produced continuously
during market hours instead of only on-demand via the API. No scan/strategy
logic is changed - this only adds the autonomous scheduling around it.

Through the service's telemetry it also logs every stock analysed (its own
scans and user-initiated ones) and what changed between scans (stocks
entering or leaving the super-performer, Stage 2, VCP and RS-leader lists).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from src.agent.base import BaseAgent
from src.agent.market_hours import is_market_open
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.agent.telemetry import Telemetry
    from src.api.service import TradingService

logger = get_logger(__name__)

DEFAULT_SCAN_INTERVAL_SECONDS = 180
IDLE_POLL_SECONDS = 30  # how often to re-check market-hours while closed
# Bound a single scan so a hang can't stall the loop forever. None = sized to the
# universe: the first scan of the day downloads one daily history per stock
# through the 3 req/s historical limit (~170 s for NIFTY 500); later scans that
# day only refresh today's bar from quotes and take seconds.
SCAN_TIMEOUT_SECONDS: "float | None" = None
PORTFOLIO_WARM_SECONDS = 600   # keep held / traded stocks analysed (incl. those outside the universe)
RESEARCH_WARM_SECONDS = 1500   # pre-fetch news / reports for held stocks so their pages open instantly

# report lists whose membership changes are worth telling the user about
WATCHED_LISTS = {
    "super_performers": "super performers",
    "stage_2_advancing": "Stage 2 (advancing)",
    "vcp_setups": "VCP setups",
    "rs_leaders": "RS leaders",
}


class ScannerAgent(BaseAgent):
    """Runs ``service.run_analysis_scan`` on an interval while the market is open."""

    NAME = "scanner"
    ROUTINE_STATES = frozenset({"scanning", "waiting_next_cycle"})

    def __init__(
        self,
        zerodha_id: str,
        service: "TradingService",
        interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS,
        universe: str = "nifty500",
    ) -> None:
        super().__init__(zerodha_id, service, interval_seconds)
        self.universe = universe
        self.last_scan_at: Optional[str] = None
        self.scan_count = 0
        self.progress: Optional[dict[str, Any]] = None
        self.last_changes: list[str] = []
        self._own_scan = False
        self._lists: dict[str, dict[str, set[str]]] = {}   # universe -> list name -> symbols
        self._portfolio_warmed_mono = 0.0
        self._research_warmed_mono = 0.0
        self.portfolio_symbols: list[str] = []

    def scan_timeout(self) -> float:
        if SCAN_TIMEOUT_SECONDS:
            return SCAN_TIMEOUT_SECONDS
        from src.analysis.nifty500 import NIFTY_500_SYMBOLS
        from src.utils.config import get_settings

        n = len(NIFTY_500_SYMBOLS) if self.universe == "nifty500" else 50
        return round(n / max(get_settings().rate_limit_historical, 0.5) * 1.3 + 60)

    async def _warm_portfolio(self) -> None:
        """Keep the user's holdings / position underlyings analysed and their research cached."""
        warm = getattr(self.service, "warm_portfolio_analysis", None)
        if warm is None or time.monotonic() - self._portfolio_warmed_mono < PORTFOLIO_WARM_SECONDS:
            return
        self._portfolio_warmed_mono = time.monotonic()
        self.activity.set_task("Refreshing analysis for held and traded stocks", "act")
        try:
            res = await asyncio.wait_for(warm(), timeout=300)
        except Exception as e:
            self.activity.trace(f"Portfolio warm-up failed: {e}", "warning", kind="warm")
            return
        self.portfolio_symbols = res.get("symbols", [])
        self.activity.work(len(res.get("refreshed", [])))
        refreshed, failed = res.get("refreshed", []), res.get("failed", [])
        msg = (f"Portfolio warm-up: {len(self.portfolio_symbols)} stocks, refreshed {len(refreshed)}"
               + (f" ({', '.join(refreshed[:8])})" if refreshed else "")
               + (f", failed {', '.join(failed)}" if failed else ""))
        if refreshed or failed:
            self.activity.record("info", msg, "warning" if failed else "info")
        else:
            self.activity.trace(msg + " — all already fresh", kind="warm")
        await self._warm_research()

    async def _warm_research(self) -> None:
        if not self.portfolio_symbols or time.monotonic() - self._research_warmed_mono < RESEARCH_WARM_SECONDS:
            return
        try:
            from src.agent.mcp_driver import MCPDriverSupervisorAgent

            driver = MCPDriverSupervisorAgent.get_instance()
            if not driver.research_agent_running:
                return
        except Exception:
            return
        self._research_warmed_mono = time.monotonic()
        done = 0
        for sym in self.portfolio_symbols:
            self.activity.set_task(f"Pre-fetching research for {sym}", "act")
            try:
                await asyncio.wait_for(driver.call_research_tool("get_stock_research", symbol=sym), timeout=60)
                done += 1
                self.activity.trace(f"Research cached for {sym}", kind="warm", symbol=sym)
            except Exception as e:
                self.activity.trace(f"Research pre-fetch failed for {sym}: {e}", "warning", kind="warm")
            await asyncio.sleep(1)   # be gentle with the news / filings sites
        self.activity.trace(f"Research pre-fetched for {done}/{len(self.portfolio_symbols)} held stocks", "info", kind="warm")

    # ── telemetry ──────────────────────────────────────────────────────
    def _subscribe(self, telemetry: "Telemetry") -> None:
        self._listen("scan.", self._on_scan_event)

    def _on_scan_event(self, topic: str, d: dict[str, Any]) -> None:
        who = "" if self._own_scan else " (user-initiated)"
        universe = str(d.get("universe", "")).upper()
        if topic == "scan.started":
            self.progress = {"scanned": 0, "total": None, "symbol": None, "universe": universe}
            self.activity.trace(f"Scan started · {universe}{who}", "info", kind="scan")
            self.activity.set_task(f"Preparing {universe} scan — resolving instruments", "observe")
        elif topic == "scan.progress":
            self.progress = {"scanned": d.get("scanned"), "total": d.get("total"), "symbol": d.get("symbol"), "universe": universe}
            self.activity.work(1)
            self.activity.trace(f"Analysed {d.get('symbol')} ({d.get('scanned')}/{d.get('total')})", kind="analyse",
                                symbol=d.get("symbol"))
            self.activity.set_task(f"Analysing {d.get('symbol')} · {d.get('scanned')}/{d.get('total')}", "observe")
        elif topic == "scan.completed":
            self.progress = None
            report = d.get("report") or {}
            if report.get("scanned"):
                self.activity.set_task("Comparing results with the previous scan", "decide")
                self._journal_changes(str(d.get("universe")), report)
                summary = ", ".join(f"{len(report.get(k) or [])} {label}" for k, label in WATCHED_LISTS.items())
                self.activity.trace(f"Scan finished{who}: {report.get('analyzed_stocks') or report.get('total_stocks')} stocks — {summary}",
                                    "info", kind="scan")
            elif report.get("error"):
                self.activity.trace(f"Scan ended with an error{who}: {report.get('error')}", "error", kind="scan")

    def _journal_changes(self, universe: str, report: dict[str, Any]) -> None:
        current = {k: {s.get("symbol") for s in (report.get(k) or []) if s.get("symbol")} for k in WATCHED_LISTS}
        previous = self._lists.get(universe)
        self._lists[universe] = current
        if previous is None:
            self.last_changes = [f"Baseline: {len(current[k])} {label}" for k, label in WATCHED_LISTS.items()]
            self.activity.record("change", "Baseline recorded — " + "; ".join(self.last_changes))
            return
        changes = []
        for key, label in WATCHED_LISTS.items():
            added = sorted(current[key] - previous[key])
            dropped = sorted(previous[key] - current[key])
            if added:
                changes.append(f"New {label}: {', '.join(added[:8])}{' …' if len(added) > 8 else ''}")
            if dropped:
                changes.append(f"Left {label}: {', '.join(dropped[:8])}{' …' if len(dropped) > 8 else ''}")
        self.last_changes = changes or ["No changes since the previous scan"]
        for c in changes:
            self.activity.record("change", c, "info")

    # ── loop ───────────────────────────────────────────────────────────
    async def _run_loop(self) -> None:
        while self._alive:
            if getattr(self.service, "is_shared_market", False):
                # view-only session: the source account scans once for everyone (src/api/published.py)
                self._set_state("using_shared_scan")
                await self._wait(IDLE_POLL_SECONDS)
                continue
            if not is_market_open():
                self._set_state("waiting_market_open")
                await self._wait(IDLE_POLL_SECONDS)
                continue
            if not self.service.is_authenticated:
                self._set_state("waiting_zerodha_auth")
                await self._wait(IDLE_POLL_SECONDS)
                continue
            self._set_state("scanning")
            self.activity.cycle_start()
            self.activity.set_task(f"Starting {self.universe.upper()} scan", "observe")
            self._own_scan = True
            timeout = self.scan_timeout()
            try:
                result = await asyncio.wait_for(
                    self.service.run_analysis_scan(universe=self.universe),
                    timeout=timeout,
                )
                if result.get("error"):
                    self.last_error = result["error"]
                    self.activity.cycle_end(False, f"Scan returned an error: {result['error']}")
                else:
                    self.last_error = None
                    self.scan_count += 1
                    n = result.get("analyzed_stocks") or result.get("total_stocks")
                    self.activity.cycle_end(True, f"Scan complete — {n} stocks analysed", stocks=n)
                    self.activity.note_result(f"Scanned {n} stocks — {self.last_changes[0] if self.last_changes else 'no changes'}")
                self.last_scan_at = datetime.now().isoformat()
            except asyncio.TimeoutError:
                self.last_error = f"scan timed out after {timeout:g}s"
                logger.error("scanner_agent_scan_timeout", zerodha_id=self.zerodha_id, timeout=timeout)
                done = f" after {self.progress['scanned']}/{self.progress['total']} stocks" if self.progress and self.progress.get("total") else ""
                self.activity.cycle_end(False, f"Scan stopped at the {timeout:g}s timeout{done} — stocks analysed so far are kept in the cache")
                self.activity.note_result(f"Last scan hit the {timeout:g}s timeout{done}", "error")
            except Exception as e:
                self.last_error = str(e)
                logger.error("scanner_agent_scan_failed", zerodha_id=self.zerodha_id, error=str(e))
                self.activity.cycle_end(False, f"Scan failed: {e}")
            finally:
                self._own_scan = False
                self.progress = None
            persist = getattr(self.service, "persist_analysis", None)
            if persist is not None:
                await persist(force=True)    # also covers a scan cut off by the timeout
            await self._warm_portfolio()
            self._set_state("waiting_next_cycle")
            await self._wait(self.interval_seconds)

    def _status_extra(self) -> dict[str, Any]:
        return {
            "last_scan_at": self.last_scan_at,
            "scan_count": self.scan_count,
            "universe": self.universe,
            "timeout_seconds": self.scan_timeout(),
            "portfolio_symbols": self.portfolio_symbols,
            "progress": self.progress,
            "last_changes": self.last_changes,
        }
