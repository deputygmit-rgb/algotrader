"""Market replay: pause / resume / speed while it runs."""
from src.api.service import TradingService


def _svc(running=True):
    svc = TradingService.__new__(TradingService)
    svc._replay_state = {"running": running, "speed": 10.0}
    return svc


def test_pause_resume_and_speed():
    svc = _svc()
    assert svc.control_replay("pause")["paused"] is True
    assert svc.control_replay("resume")["paused"] is False
    assert svc.control_replay("speed", 50)["speed"] == 50
    assert svc.control_replay("speed", 10_000)["speed"] == 200          # capped
    assert svc.control_replay("speed", 0.1)["speed"] == 0.5             # floored
    assert "error" in svc.control_replay("rewind")


def test_no_replay_running():
    assert "error" in _svc(running=False).control_replay("pause")
