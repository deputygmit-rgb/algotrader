"""Agent visibility: activity journal, change-only findings, feed health and crash state."""
import asyncio
from types import SimpleNamespace

import pytest

from src.agent import executor_agent, scanner_agent
from src.agent.activity import AgentActivity
from src.agent.executor_agent import ExecutorAgent
from src.agent.scanner_agent import ScannerAgent
from src.agent.tracker_agent import TrackerAgent
from src.agent.verifier_agent import VerifierAgent


def test_activity_cycles_timings_and_quiet_cycles():
    a = AgentActivity()
    a.cycle_start()
    a.cycle_end(True, "done")
    a.cycle_start()
    a.cycle_end(False, "boom")
    a.cycle_start()
    a.cycle_end(True, "routine", record=False)
    snap = a.snapshot()
    assert snap["cycles"] == 3 and snap["failures"] == 1
    assert len(snap["durations"]) == 3
    assert [e["message"] for e in snap["events"]] == ["done", "boom"]   # quiet cycle not journaled
    assert snap["events"][1]["level"] == "error"
    a.waiting(30)
    assert a.snapshot()["next_run_at"] is not None


class _Svc:
    def __init__(self):
        self.is_running = True
        self.is_authenticated = True
        self._ticker = SimpleNamespace(is_connected=False)
        self._last_tick_at = None
        self._tick_count = 0


@pytest.mark.asyncio
async def test_executor_journals_feed_transitions_and_outage(monkeypatch):
    monkeypatch.setattr(executor_agent, "is_market_open", lambda: True)
    svc = _Svc()
    ex = ExecutorAgent("U1", svc, interval_seconds=0.01)
    ex.start()
    await asyncio.sleep(0.05)                       # several checks while disconnected
    svc._ticker.is_connected = True
    svc._last_tick_at = __import__("time").time()
    await asyncio.sleep(0.05)
    ex.stop()
    st = ex.status()
    msgs = [e["message"] for e in st["activity"]["events"] if e["kind"] in ("finding", "info")]
    assert sum("disconnected" in m for m in msgs) == 1     # one entry, not one per check
    assert any(m.startswith("Feed restored after") for m in msgs)
    assert st["feed_status"] == "streaming" and len(st["outages"]) == 1
    assert st["activity"]["cycles"] >= 4


@pytest.mark.asyncio
async def test_verifier_journals_only_when_failing_checks_change():
    svc = _Svc()
    v = VerifierAgent("U1", svc, interval_seconds=0.01)

    def report(failing):
        checks = [SimpleNamespace(name=n, message=f"{n} at {i}", blocks_execution=n in failing)
                  for i, n in enumerate(["broker", "feed", "margin"])]
        return SimpleNamespace(passed=not failing, checks=checks, blocked_reasons=[c.message for c in checks if c.blocks_execution],
                               to_dict=lambda: {"passed": not failing, "checks": [], "blocked_reasons": []})

    seq = [report(set()), report({"feed"}), report({"feed"}), report({"feed"}), report({"feed", "margin"}), report(set())]

    class Checker:
        async def run_all_checks(self, signal=None, mode=None):
            return seq.pop(0) if len(seq) > 1 else seq[0]

    v._checker = Checker()
    v._alive = True
    task = asyncio.create_task(v._run_loop())
    await asyncio.sleep(0.12)
    v.stop()
    await asyncio.sleep(0)
    ev = [(e["kind"], e["message"]) for e in v.status()["activity"]["events"] if e["kind"] != "state"]
    findings = [m for k, m in ev if k == "finding"]
    assert findings == ["feed at 1", "margin at 2"]          # repeats of "feed" not re-journaled
    assert any(m == "Resolved: feed" for _, m in ev) and any(m == "Resolved: margin" for _, m in ev)
    task.cancel()


@pytest.mark.asyncio
async def test_crashed_agent_reports_crashed_not_running():
    svc = _Svc()
    tr = TrackerAgent("U1", svc, interval_seconds=0.01)

    async def boom():
        raise RuntimeError("loop exploded")

    tr._run_loop = boom
    tr.start()
    await asyncio.sleep(0.02)
    st = tr.status()
    assert st["running"] is False and st["state"] == "crashed"
    assert st["activity"]["events"][-1]["message"].startswith("Agent crashed")


@pytest.mark.asyncio
async def test_scanner_journals_timeout_as_failed_cycle(monkeypatch):
    monkeypatch.setattr(scanner_agent, "is_market_open", lambda: True)
    monkeypatch.setattr(scanner_agent, "SCAN_TIMEOUT_SECONDS", 0.01)
    svc = _Svc()

    async def slow_scan(universe):
        await asyncio.sleep(1)
        return {}

    svc.run_analysis_scan = slow_scan
    sc = ScannerAgent("U1", svc, interval_seconds=10)
    sc.start()
    await asyncio.sleep(0.05)
    sc.stop()
    act = sc.status()["activity"]
    assert act["failures"] == 1 and act["cycles"] == 1
    assert "timeout" in act["events"][-2]["message"] or any("timeout" in e["message"] for e in act["events"])
    assert act["next_run_at"] is not None
