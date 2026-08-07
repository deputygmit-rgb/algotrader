"""End-to-end tests (mocked broker) for the Scanner/Verifier/Executor/Tracker
pipeline agents: state transitions, error handling, and operation timeouts.

These exercise the real agent code paths (``_run_loop`` via ``start()``) against
a fake ``TradingService``, so no real Zerodha/network calls are made.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.executor_agent import ExecutorAgent
from src.agent.scanner_agent import ScannerAgent
from src.agent.tracker_agent import TrackerAgent
from src.agent.verifier_agent import VerifierAgent


class FakeTicker:
    """Mirrors the real KiteTicker's is_connected being a @property, not a method."""

    def __init__(self, connected: bool = True) -> None:
        self._connected = connected

    @property
    def is_connected(self) -> bool:
        return self._connected


class FakeService:
    """Minimal stand-in for TradingService with controllable async behavior."""

    def __init__(self) -> None:
        self.is_authenticated = True
        self.is_running = True
        self._client = MagicMock()
        self._ticker = FakeTicker(connected=True)
        self._risk = MagicMock()
        self._risk._total_exposure = 0.0  # real float: preflight does numeric comparisons on this
        self._risk.is_kill_switch_active = False
        self.run_analysis_scan = AsyncMock(return_value={"opportunities": []})
        self.get_positions = AsyncMock(return_value={"net": []})
        self.get_orders = AsyncMock(return_value=[])


async def _run_until(condition, timeout: float = 2.0) -> None:
    """Poll until condition() is truthy or fail with a clear timeout message."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


# ─────────────────────────────────────────────────────────
# ScannerAgent
# ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scanner_waits_when_market_closed(monkeypatch):
    monkeypatch.setattr("src.agent.scanner_agent.is_market_open", lambda: False)
    service = FakeService()
    agent = ScannerAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["state"] == "waiting_market_open")
        assert service.run_analysis_scan.await_count == 0
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_scanner_waits_when_not_authenticated(monkeypatch):
    monkeypatch.setattr("src.agent.scanner_agent.is_market_open", lambda: True)
    service = FakeService()
    service.is_authenticated = False
    agent = ScannerAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["state"] == "waiting_zerodha_auth")
        assert service.run_analysis_scan.await_count == 0
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_scanner_success_updates_state_and_counters(monkeypatch):
    monkeypatch.setattr("src.agent.scanner_agent.is_market_open", lambda: True)
    service = FakeService()
    agent = ScannerAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["scan_count"] >= 1)
        status = agent.status()
        assert status["last_error"] is None
        assert status["last_scan_at"] is not None
        assert status["state"] == "waiting_next_cycle"
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_scanner_handles_exception_without_dying(monkeypatch):
    monkeypatch.setattr("src.agent.scanner_agent.is_market_open", lambda: True)
    service = FakeService()
    service.run_analysis_scan = AsyncMock(side_effect=RuntimeError("boom"))
    agent = ScannerAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["last_error"] == "boom")
        assert agent.status()["running"] is True  # loop kept going
        assert not agent._task.done()
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_scanner_bounds_a_hanging_scan_with_timeout(monkeypatch):
    monkeypatch.setattr("src.agent.scanner_agent.is_market_open", lambda: True)
    monkeypatch.setattr("src.agent.scanner_agent.SCAN_TIMEOUT_SECONDS", 0.05)
    service = FakeService()

    async def _hang(universe: str):
        await asyncio.sleep(10)
        return {}

    service.run_analysis_scan = _hang
    agent = ScannerAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: "timed out" in (agent.status()["last_error"] or ""), timeout=2.0)
        assert agent.status()["running"] is True
    finally:
        agent.stop()


# ─────────────────────────────────────────────────────────
# VerifierAgent
# ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verifier_idles_until_live_trading():
    service = FakeService()
    service.is_running = False
    agent = VerifierAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["state"] == "waiting_live_trading")
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_verifier_runs_checks_and_records_report():
    service = FakeService()
    agent = VerifierAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["last_checked_at"] is not None)
        assert agent.status()["last_report"] is not None
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_verifier_handles_check_exception():
    service = FakeService()
    agent = VerifierAgent("U1", service, interval_seconds=5)
    agent.start()
    await _run_until(lambda: agent._checker is not None)
    agent._checker.run_all_checks = AsyncMock(side_effect=ValueError("bad data"))
    try:
        await _run_until(lambda: agent.status()["state"] == "waiting_next_cycle")
        assert agent.status()["running"] is True
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_verifier_bounds_a_hanging_check_with_timeout(monkeypatch):
    monkeypatch.setattr("src.agent.verifier_agent.CHECK_TIMEOUT_SECONDS", 0.05)
    service = FakeService()
    agent = VerifierAgent("U1", service, interval_seconds=5)
    agent.start()
    await _run_until(lambda: agent._checker is not None)

    async def _hang(signal, mode):
        await asyncio.sleep(10)

    agent._checker.run_all_checks = _hang
    try:
        await _run_until(lambda: agent.status()["state"] == "waiting_next_cycle", timeout=2.0)
        assert agent.status()["running"] is True
    finally:
        agent.stop()


# ─────────────────────────────────────────────────────────
# ExecutorAgent
# ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_executor_idles_until_live_trading():
    service = FakeService()
    service.is_running = False
    agent = ExecutorAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["state"] == "waiting_live_trading")
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_executor_detects_ticker_disconnect():
    service = FakeService()
    service._ticker = FakeTicker(connected=False)
    agent = ExecutorAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["stall_warnings"] >= 1)
        assert agent.status()["ticker_connected"] is False
        assert agent.status()["running"] is True
        assert agent.status()["last_error"] is None
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_executor_is_connected_property_not_called_as_method():
    # Regression: KiteTicker.is_connected is a @property; calling it as a
    # method used to raise "bool object is not callable", silently swallowed,
    # leaving ticker_connected stuck at its default False/stall_warnings at 0.
    service = FakeService()
    service._ticker = FakeTicker(connected=True)
    agent = ExecutorAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["last_checked_at"] is not None)
        status = agent.status()
        assert status["ticker_connected"] is True
        assert status["last_error"] is None
        assert status["stall_warnings"] == 0
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_executor_handles_check_exception():
    class BrokenTicker:
        @property
        def is_connected(self) -> bool:
            raise RuntimeError("ticker check failed")

    service = FakeService()
    service._ticker = BrokenTicker()
    agent = ExecutorAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["last_error"] == "ticker check failed")
        assert agent.status()["running"] is True
    finally:
        agent.stop()


# ─────────────────────────────────────────────────────────
# TrackerAgent
# ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tracker_idles_until_live_trading():
    service = FakeService()
    service.is_running = False
    agent = TrackerAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["state"] == "waiting_live_trading")
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_tracker_success_updates_counts():
    service = FakeService()
    service.get_positions = AsyncMock(return_value={"net": [{"quantity": 5}, {"quantity": 0}]})
    service.get_orders = AsyncMock(return_value=[{"status": "OPEN"}, {"status": "COMPLETE"}])
    agent = TrackerAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["last_synced_at"] is not None)
        status = agent.status()
        assert status["open_positions_count"] == 1
        assert status["open_orders_count"] == 1
        assert status["last_error"] is None
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_tracker_handles_exception_without_dying():
    service = FakeService()
    service.get_positions = AsyncMock(side_effect=ConnectionError("network down"))
    agent = TrackerAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: agent.status()["last_error"] == "network down")
        assert agent.status()["running"] is True
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_tracker_bounds_a_hanging_sync_with_timeout(monkeypatch):
    monkeypatch.setattr("src.agent.tracker_agent.SYNC_TIMEOUT_SECONDS", 0.05)
    service = FakeService()

    async def _hang():
        await asyncio.sleep(10)
        return {}

    service.get_positions = _hang
    agent = TrackerAgent("U1", service, interval_seconds=5)
    agent.start()
    try:
        await _run_until(lambda: "timed out" in (agent.status()["last_error"] or ""), timeout=2.0)
        assert agent.status()["running"] is True
    finally:
        agent.stop()


# ─────────────────────────────────────────────────────────
# wake() should short-circuit the wait immediately
# ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wake_triggers_immediate_recheck(monkeypatch):
    service = FakeService()
    service.is_running = False
    agent = TrackerAgent("U1", service, interval_seconds=100)  # long interval
    agent.start()
    try:
        await _run_until(lambda: agent.status()["state"] == "waiting_live_trading")
        service.is_running = True
        agent.wake()
        await _run_until(lambda: agent.status()["last_synced_at"] is not None, timeout=1.0)
    finally:
        agent.stop()
