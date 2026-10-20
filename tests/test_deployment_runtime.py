"""Runtime behaviour the VPS deployment depends on: exchange-time clock,
logging configured under uvicorn, and live strategies surviving restarts."""

import json
import os
import subprocess
import sys

import pytest

from src.api.service import TradingService


def test_process_runs_on_exchange_time_even_on_a_utc_server():
    # a fresh interpreter whose environment says UTC, as on a cloud VPS
    code = ("from src.utils.clock import use_exchange_timezone; import time; "
            "use_exchange_timezone(); print(time.strftime('%z'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "TZ": "UTC"}, cwd=os.getcwd(), check=True)
    assert out.stdout.strip() == "+0530"


def test_importing_the_web_app_configures_logging_and_the_clock():
    code = ("import logging, time; import src.api.webapp; "
            "print(len(logging.getLogger().handlers), time.strftime('%z'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "TZ": "UTC"}, cwd=os.getcwd(), check=True)
    handlers, offset = out.stdout.strip().splitlines()[-1].split()
    assert int(handlers) >= 2 and offset == "+0530"     # stdout + file (+ event db)


@pytest.fixture
def strategies_path(tmp_path, monkeypatch):
    path = tmp_path / "TESTUSER" / "live_strategies.json"

    def fake_file(self):
        return None if self.is_demo else str(path)

    monkeypatch.setattr(TradingService, "_strategies_file", fake_file)
    return path


def test_live_strategies_survive_a_restart(strategies_path):
    svc = TradingService(zerodha_id="TESTUSER")
    assert svc.add_strategy("day_trade_setups").get("success")
    assert svc.add_strategy("rsi", {"tradingsymbol_map": {408065: "INFY", "408065": "INFY"}}).get("success")
    svc.toggle_strategy("day_trade_setups")                       # paused

    saved = json.loads(strategies_path.read_text())
    assert [r["name"] for r in saved] == ["day_trade_setups", "rsi"]

    restarted = TradingService(zerodha_id="TESTUSER")
    info = {s["name"]: s for s in restarted.get_strategies_info()}
    # restored under the registry name it was added with ("rsi"); the strategy names itself rsi_strategy
    assert set(info) == {"day_trade_setups", "rsi_strategy"}
    assert info["day_trade_setups"]["is_active"] is False
    assert restarted._strategy_tokens() == [408065]               # re-subscribed when live starts

    restarted.remove_strategy("day_trade_setups")
    assert [r["name"] for r in json.loads(strategies_path.read_text())] == ["rsi"]


def test_demo_services_do_not_persist(strategies_path):
    svc = TradingService(zerodha_id="DEMO-P-abc", demo=True)
    svc.add_strategy("day_trade_setups")
    assert not strategies_path.exists()
