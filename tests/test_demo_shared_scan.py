"""Demo sessions share one scanner: the simulated market is the same for all of
them, so one scan serves every session (src/api/service.py)."""

import asyncio

import pytest

import src.api.service as service_mod
from src.api.service import TradingService


class FakeScanner:
    def __init__(self):
        self.calls = []
        self._scanning = False

    @property
    def is_scanning(self):
        return self._scanning

    async def run_scan(self, client, symbols=None, universe="nifty50", on_progress=None):
        self.calls.append((tuple(symbols or ()), universe))
        self._scanning = True
        try:
            await asyncio.sleep(0.05)
            return {"scanned": True, "universe": universe, "n": len(self.calls)}
        finally:
            self._scanning = False


@pytest.fixture
def shared(monkeypatch):
    scanner = FakeScanner()
    monkeypatch.setattr(service_mod, "_DEMO_SCANNER", scanner)
    monkeypatch.setattr(service_mod, "_DEMO_SCAN_REPORTS", {})
    return scanner


def _demo():
    svc = TradingService.__new__(TradingService)
    svc.is_demo = True
    svc._scanner = service_mod._shared_demo_scanner()
    svc._client = object()
    svc._ws_clients = []
    return svc


def test_demo_sessions_share_one_scanner_real_accounts_do_not(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(service_mod, "_DEMO_SCANNER", None)
    a = TradingService(zerodha_id="DEMO-P-AAAA0001", demo=True)
    b = TradingService(zerodha_id="DEMO-P-BBBB0002", demo=True)
    assert a._scanner is b._scanner
    real = TradingService(zerodha_id="AB1234")
    assert real._scanner is not a._scanner


def test_concurrent_demo_scans_run_once(shared):
    async def go():
        return await asyncio.gather(*(_demo().run_analysis_scan(universe="nifty500") for _ in range(4)))

    results = asyncio.run(go())
    assert shared.calls == [((), "nifty500")]
    assert all(r == results[0] for r in results)


def test_recent_scan_is_reused_then_refreshed(shared, monkeypatch):
    asyncio.run(_demo().run_analysis_scan(universe="nifty500"))
    asyncio.run(_demo().run_analysis_scan(universe="nifty500"))
    assert len(shared.calls) == 1
    asyncio.run(_demo().run_analysis_scan(universe="nifty50"))       # other universe: own scan
    assert len(shared.calls) == 2
    monkeypatch.setattr(service_mod, "DEMO_SCAN_REUSE_SECONDS", 0.0)
    asyncio.run(_demo().run_analysis_scan(universe="nifty500"))       # stale: scans again
    assert len(shared.calls) == 3


def test_symbol_scans_are_not_served_from_the_shared_report(shared):
    asyncio.run(_demo().run_analysis_scan(universe="nifty500"))
    asyncio.run(_demo().run_analysis_scan(symbols=["INFY"], universe="nifty500"))
    assert shared.calls[-1] == (("INFY",), "nifty500")


def test_demo_does_not_write_analysis_snapshots(shared):
    svc = _demo()
    svc._snapshot_dirty = True
    assert asyncio.run(svc.persist_analysis(force=True)) is False
