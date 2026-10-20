# Kite Trading Agent

## Overview
Production-grade algorithmic trading agent with interactive web dashboard, built in Python. Fully integrated with Zerodha Kite Connect API v3. Supports live trading, options strategies, backtesting, and real-time risk management.

## Architecture
```
src/
  auth/
    authenticator.py - Per-user Kite OAuth with isolated token files
    otp_auth.py      - OTP login with session-to-zerodha_id binding
    user_config.py   - UserConfigService: Zerodha ID -> API key/secret + access level (data/users.json)
    access.py        - Access levels (view / trade / agentic) and the order-placement rule
  api/
    client.py     - Async REST client wrapping all Kite Connect v3 endpoints
    ticker.py     - WebSocket client for real-time market data
    service.py    - TradingService (per-user) + TradingServiceManager (instance cache)
    shared_market.py - Read-only live market data for view-only accounts (MARKET_DATA_ACCOUNT)
    webapp.py     - FastAPI web application with REST + WebSocket endpoints (user-scoped)
  analysis/
    indicators.py - Technical indicators (SMA, EMA, RSI, MACD, ADX, ATR, BB, volume ratio)
    scanner.py    - Stock scanner with super performance criteria, trigger detection
  strategy/       - Pluggable strategy framework (EMA, VWAP, MeanRev, RSI)
  options/
    greeks.py     - Black-Scholes Greeks/IV engine
    chain.py      - Option chain builder with max pain, PCR
    strategies.py - Iron Condor, Straddle/Strangle, Bull/Bear Spreads
  execution/      - Smart order routing, slippage protection, reconciliation
  risk/           - Risk management, position sizing, kill switch
  data/           - Pydantic models, backtest engine, trade journal
  utils/          - Config, structured logging, exceptions
templates/        - Jinja2 HTML templates (dashboard)
static/           - CSS + JS for frontend
main.py           - FastAPI server entry point (port 5001)
```

## Multi-User Architecture
- **UserConfigService**: Maps Zerodha IDs to API key/secret pairs, stored in `data/users.json`
- **Per-user KiteAuthenticator**: Each user gets own authenticator with own API credentials and token file (`.kite_session_{zerodha_id}.json`)
- **TradingServiceManager**: Caches per-user TradingService instances keyed by Zerodha ID
- **Session binding**: OTP sessions store `zerodha_id`, all API routes resolve user from session cookie
- **Data isolation**: Each user gets their own TradingService with own KiteClient, strategies, risk manager, etc.
- **Login flow**: Requires Zerodha ID + email, validates against UserConfigService before issuing OTP

## Access Levels
Every account has one level in `data/users.json`, set with `scripts/manage_users.py`:

| Level | Allows |
|---|---|
| `view` | Dashboards, signals, analysis, backtests, paper trading. No real orders. |
| `trade` | Also real orders the user triggers by hand (Execute on a stock or OI signal). Orders placed automatically are refused; protective orders on open positions (stop-loss, target, square-off, close) are allowed. |
| `agentic` | Also automated orders: live strategies, Day Trade auto-trade, agents. |

- Accounts created before levels existed are `agentic`; new accounts start at `view`. Demo sessions are `agentic` (simulated broker); an unknown account is `view`.
- **Enforcement:** every real order passes `KiteClient.place_order` / `modify_order`, which checks the account's level against the order's origin (`manual`, `risk` or `automated`; unmarked orders count as `automated`). The origin travels through the order queue; the engine marks its protective orders `risk`.
- **API:** adding/promoting/resuming a live strategy and switching auto-trade on need `agentic`; Execute needs `trade`. Refusals return HTTP 403 with `access_denied: true`.
- **UI:** header badge shows the level; controls above it are dimmed and explain why when clicked.
- Commands (key and secret are asked at a hidden prompt; a `view` account may skip them and use the shared market view): `python scripts/manage_users.py add <ID> <EMAIL> --access view|trade|agentic`, `python scripts/manage_users.py access <ID> <level>`, `python scripts/manage_users.py list`. Full server commands: [deploy/DEPLOY.md](deploy/DEPLOY.md).

## Shared Market View
View-only accounts that have not connected their own Zerodha account see live market data from one designated account's Kite session (`MARKET_DATA_ACCOUNT` in `.env`). Without it, or on days that account is not logged in to Kite, they get the demo market.

- Viewers get only the instrument list, quotes/OHLC/LTP, historical candles and live prices. Account calls (profile, margins, orders, trades, positions, holdings, GTT) answer from the viewer's own empty account and never reach the source account; any other client call does not exist.
- Viewers never hold the source credentials and cannot log in, log out or invalidate its session. Errors shown to viewers are generic.
- Live prices come from one extra WebSocket for all viewers (Kite allows 3 per API key); order updates on it are dropped. Each viewer receives only instruments it subscribed to (1,000 each, 3,000 total).
- Viewers share half of the source account's rate limits behind a short cache, so they cannot slow its own trading.
- Shared sessions are always view-only and use ids `VIEW-<ID>`, which cannot collide with a Zerodha ID. `/api/status` reports `market_feed: "shared"` and never names the source account.
- Check Kite Connect / exchange data terms before enabling it in production.

## Access Requests
The landing page and demo "Request access" forms ask for the access level, the broker and a confirmation. The admin email shows the request and the ready `manage_users.py add ... --access <level>` command; the requester gets a follow-up email with next steps (Kite Connect app, redirect URL `<PUBLIC_URL>/api/auth/callback`, what not to send by email). Requests are stored in `contact_requests` (metrics DB; `access_level`, `platform` columns are added automatically on start). Report: `python scripts/metrics_report.py`.

## Deployment
Production runs on an Ubuntu 24.04 VPS (verquoai.com) under systemd + nginx. See [deploy/DEPLOY.md](deploy/DEPLOY.md).

## Key Components
- **Web Dashboard**: Interactive UI with live market data, positions, orders, signals, strategy management, risk controls, stock analysis
- **KiteClient**: Async REST client with rate limiting, retry logic, all Kite v3 endpoints
- **KiteTicker**: WebSocket client with binary tick parsing, auto-reconnection, heartbeat keep-alive
- **Options Module**: Black-Scholes Greeks, IV calculation, option chain builder, max pain
- **Analysis Module**: Technical indicators (SMA, EMA, RSI, MACD, ADX, ATR, Bollinger Bands), super performance stock scanner (Minervini Trend Template), daily/weekly/monthly gainers & losers, trigger detection
- **8 Strategies**: EMA Crossover, VWAP Breakout, Mean Reversion, RSI, Iron Condor, Straddle/Strangle, Bull Call Spread, Bear Put Spread
- **RiskManager**: Daily loss limits, position sizing, exposure control, kill switch
- **ExecutionEngine**: Order routing, slippage protection, partial fill handling, reconciliation loop
- **BacktestEngine**: Vectorized backtesting with Sharpe, CAGR, drawdown, win rate
- **TradeJournal**: PnL tracking by instrument and strategy

## Tech Stack
- Python 3.11, FastAPI, uvicorn
- asyncio, aiohttp, websockets
- Pydantic v2, pydantic-settings
- pandas, numpy, scipy (Black-Scholes)
- structlog, aiolimiter
- Jinja2, vanilla JS frontend

## API Endpoints
- `GET /` - Dashboard
- `GET /api/status` - Full system status
- `GET /api/profile`, `/api/margins`, `/api/orders`, `/api/positions`, `/api/holdings`
- `POST /api/strategies/add`, `/api/strategies/remove`, `/api/strategies/toggle`
- `POST /api/live/start`, `/api/live/stop`
- `GET /api/signals`, `/api/ticks`, `/api/risk`, `/api/journal`
- `GET /api/options/chain`
- `GET /api/oi/summary?underlying=NIFTY` - OI tracker summary for NIFTY/SENSEX
- `GET /api/oi/all` - Combined OI data for all tracked underlyings
- `POST /api/oi/start` - Start OI tracking with instrument registration
- `GET /api/analysis/status` - Scanner status (scanning, last_scan_time)
- `POST /api/analysis/scan` - Run full NIFTY 50 stock scan
- `GET /api/analysis/results` - Get cached analysis results
- `POST /api/risk/kill-switch`
- `WS /ws/live` - Live WebSocket stream with heartbeat keep-alive (ticks, signals, positions)
- `WS /ws/oi` - Dedicated OI WebSocket stream (continuous OI updates every 2s)

## Configuration
Environment variables via `.env` file (see `.env.template`).

## Recent Changes
- 2026-10-20: Shared market view - view-only accounts see live market data from `MARKET_DATA_ACCOUNT` with no access to that account's data, credentials or orders
- 2026-10-20: Access levels (view / trade / agentic) enforced at the broker client, the API and the UI; `manage_users.py --access` and `access` command
- 2026-10-20: Access request form asks for access level, broker and confirmation; requester gets a follow-up email with next steps; `PUBLIC_URL` setting
- 2026-10-20: VPS runtime fixes - IST clock (`TZ=Asia/Kolkata`), logging under uvicorn, live strategies persist across restarts (`data/users/<ID>/live_strategies.json`)
- 2026-02-21: Multi-user support - Zerodha ID login, per-user API credentials (data/users.json), isolated TradingService instances, session-to-user binding
- 2026-02-21: Added stock analysis module with technical indicators, super performance scanner, daily/weekly analysis, trigger detection, and Analysis tab in dashboard
- 2026-02-21: Enhanced WebSocket with heartbeat ping/pong keep-alive and exponential backoff reconnection
- 2026-02-21: Added interactive web dashboard, options trading module, FastAPI backend
- 2026-02-21: Initial build - complete trading agent with all modules
