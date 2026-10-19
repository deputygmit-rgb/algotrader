# Trading agents

Each logged-in Zerodha user gets one `TradingService` and four background agents. They are created on the first authenticated request and run as asyncio tasks in the web server process.

| Agent | Doc | Actually does | Runs when |
|---|---|---|---|
| ScannerAgent | [scanner-agent.md](scanner-agent.md) | Re-runs the NIFTY 500 technical scan every 3 min | market hours + authenticated |
| VerifierAgent | [verifier-agent.md](verifier-agent.md) | Runs the preflight checklist every 45 s; advisory only | live trading on |
| ExecutorAgent | [executor-agent.md](executor-agent.md) | Checks the Kite ticker is connected every 30 s; places no orders | live trading on |
| TrackerAgent | [tracker-agent.md](tracker-agent.md) | Fetches positions and orders every 20 s, counts and stores them | live trading on |

```
FastAPI lifespan
└─ MCPDriverSupervisorAgent            (src/agent/mcp_driver.py, one per process)
   ├─ MCPResearchAgent                 (stock-research MCP server, not covered here)
   └─ TradingServiceSupervisorAgent    (src/agent/supervisor.py)
      └─ TradingServiceAgent × users   (60 s heartbeat)
         ├─ ScannerAgent
         ├─ VerifierAgent
         ├─ ExecutorAgent
         └─ TrackerAgent
```

## Workload logs (telemetry)

`TradingService` publishes what it does on an in-process bus (`src/agent/telemetry.py`). Agents subscribe and journal it:

| Topic | Emitted when | Logged by |
|---|---|---|
| `feed.ticks`, `bar.close` | tick batch arrives / a bar closes | Executor (counts, per-cycle summary) |
| `strategy.eval`, `strategy.tick_eval`, `strategy.error` | a strategy evaluates a closed bar / ticks, or raises | Executor (one trace line per evaluation) |
| `signal.received`, `signal.ignored` | `_process_signal` gets a signal / drops it (and why) | Executor |
| `order.placed`, `order.close`, `order.failed`, `risk.blocked`, `risk.kill_switch` | execution outcome | Executor |
| `scan.started`, `scan.progress`, `scan.completed` | any analysis scan (agent or user) | Scanner (each stock, list changes between scans) |
| `board.track`, `board.signal`, `board.status`, `board.order`, `board.autotrade` | Day Trade board (Market Flow): symbol tracked / untracked, new signal, status change, auto-trade decision, settings change | Executor |
| `oi.bias`, `oi.bias_error` | every minute in market hours: NIFTY / SENSEX options-OI bias with its inputs (also stored in `oi_bias_history`) | Verifier (event when the direction changes) |

**Day Trade board auto-trade** (`src/api/board_trader.py`) is opt-in and off by default. A signal is sent only when all of these hold:

- it is new, on the latest closed bar, within one bar of its close and before 14:45;
- it isn't a replay from before tracking and wasn't skipped by the board's limit;
- it passes the side and setup filters and the daily order cap;
- live trading is running and there's no open board or day-trade position in that stock.

Quantity is `risk_per_trade / |entry − stop|`, capped by `max_order_value`. The order is the live strategy's own `build_order_signal`. The Verifier's preflight checklist runs on that exact order, then it goes through `_process_signal` like any live strategy signal.

Each signal gets one recorded decision, never retried. Decisions and the board's state are saved in `data/day_trade/<user>/`.

The Verifier traces every audit and failing check. The Tracker journals broker-side changes between syncs: positions opened, closed or resized; orders new or changing status, with the rejection reason.

Each agent writes `logs/agents/<zerodha_id>/<agent>-YYYY-MM-DD.jsonl` (60-day retention) and reloads today's file on restart.

- **In the UI:** the Agents tab console streams `GET /api/agents/log?cursor=…`.
- **Download:** `GET /api/agents/log/download?agent=<name>&day=YYYY-MM-DD` returns the raw file.

## Shared design

All four use the same pattern:

- a loop task started by `start()`;
- an `_alive` flag;
- a `_wake_event` so `wake()` cuts a wait short (called after Zerodha login);
- a `_state` string logged on change;
- a read-only `status()` served at `GET /api/agents/status`.

They read the service's state directly, and three of them reach into its private attributes (`_ticker`, `_client`, `_risk`, `_market_store`).

## Findings that span all four

1. **There is no pipeline.** The supervisor describes "scan → verify → execute → track", but the agents never exchange information. Each one only reads and writes `TradingService`. Scanner output isn't verified, Verifier findings don't gate the Executor, and Tracker snapshots feed nothing. Today they are four independent pollers. Either connect them with explicit messages (e.g. an asyncio queue or event bus with typed events), or stop describing them as a pipeline.

2. **A crashed agent stays "running".** `_on_done` logs the exception, but `_alive` stays `True`, so `status()` reports `running: true` for a dead task. The heartbeat only writes a debug log, and nothing restarts a failed agent. Fix: set `_alive = False` and a `crashed` state in `_on_done`, and have the heartbeat restart dead sub-agents with backoff.

3. **Agents outlive the user's session.** `remove_service` is only called on server shutdown. Logging out of the dashboard leaves all four agents (and the Kite token they use) running until restart.

4. **Silently not started.** If `get_service` is ever first called outside a running event loop, `start_supervision` returns early. The cached agent is then reused forever without its sub-agents ever starting. In this app that path is only reached from request handlers, so it's latent, not active.

5. **Invisible in the UI.** Only the Verifier's last report is displayed. Scanner timeouts, the Executor's 7.6-hour ticker outage and Tracker failures appear only in `logs/trading.log`.

6. **Market-hours handling is inconsistent.** The Scanner gates on an IST market-hours helper; the other three gate on "live trading is on" and keep polling the broker after 15:30. None of them knows about exchange holidays.

7. **API budget per user while live.**
   - Tracker: ~6 broker calls/min (positions + orders every 20 s).
   - Verifier: ~3–4 calls/min (margins twice per audit, profile ping cached 60 s).
   - Service's own loops: ~4/min baseline (slow reconciliation + position loop, both 30 s). The 2 s fast loop calls the broker only while orders are pending.
   - Scanner: up to 3 historical calls/second for most of each cycle.

   The historical limiter is shared with every chart, backtest and on-demand analysis the user starts.

Evidence in the per-agent docs comes from `logs/trading.log` (7,512 agent events, 7 Aug – 16 Oct 2026) and `data/market_data.db`, read on 16 Oct 2026.
