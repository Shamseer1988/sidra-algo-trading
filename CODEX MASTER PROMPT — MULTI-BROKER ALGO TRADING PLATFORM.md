# CODEX MASTER PROMPT — MULTI-BROKER ALGO TRADING PLATFORM

You are acting as a **Senior Quant Trading Systems Architect, Senior Python/FastAPI Engineer, Senior Next.js Engineer, DevOps Engineer, Database Architect, and Security Engineer**.

Your task is to design and build a **production-grade Indian stock-market Algo Trading Platform** supporting:

- Upstox API
- Firstock API
- NSE equities initially
- Future support for NFO/F&O
- Multiple trading strategies
- Live market scanner
- Telegram alerts
- Paper trading
- Assisted/manual execution
- Fully automated execution only after all safety modules are implemented and explicitly enabled
- Backtesting
- Trading journal
- Risk management
- Multi-broker order management
- Monitoring and audit logging

This application will run initially on my **Windows home PC with a Static Public IP**.

Do not create a toy/demo application.

Design this as a modular platform that can later support additional brokers such as Zerodha, Dhan, FYERS, Angel One, Shoonya, etc. without rewriting the strategy engine.

---

# 1. DEVELOPMENT PRINCIPLE

Work incrementally.

DO NOT try to build the complete project in one uncontrolled pass.

Before making major changes:

1. Inspect the existing repository.
2. Understand the current architecture.
3. Reuse existing working modules where appropriate.
4. Do not unnecessarily rewrite working code.
5. Create a clear implementation plan.
6. Implement one logical stage at a time.
7. Run tests after every major stage.
8. Fix errors before proceeding.
9. Never leave placeholder implementations inside production-critical trading modules.

If the repository is empty, initialize the project using the architecture defined below.

Maintain a running file:

`docs/IMPLEMENTATION_STATUS.md`

It must show:

- Completed
- In progress
- Pending
- Known issues
- Technical decisions
- Migration notes

---

# 2. APPLICATION MODES

The platform must support four clearly separated modes:

### SCANNER MODE

Analyze live markets and generate signals only.

No orders.

### PAPER MODE

Use real market data but simulate:

- orders
- fills
- stop losses
- targets
- slippage
- brokerage estimates
- P&L

Absolutely no broker order requests.

### ASSISTED MODE

System generates a trade.

User must explicitly approve the trade from the dashboard before execution.

### AUTO MODE

System may automatically submit orders only after:

- strategy enabled
- broker authenticated
- market feed healthy
- risk engine approval
- risk limits valid
- static-IP validation
- kill switch disabled
- daily-loss limit not exceeded
- data freshness validated

AUTO MODE must be disabled by default.

---

# 3. RECOMMENDED TECHNOLOGY STACK

## Frontend

Use:

- Next.js latest stable App Router
- TypeScript
- Tailwind CSS
- shadcn/ui
- TanStack Query
- Zustand only where appropriate
- React Hook Form
- Zod
- TradingView Lightweight Charts
- Lucide icons

The UI must support:

- Light Mode
- Dark Mode

Both themes must be fully designed.

Do not create a dark theme by simply inverting colors.

---

# 4. BACKEND

Use:

- Python
- FastAPI
- Pydantic
- SQLAlchemy 2.x
- Alembic
- async architecture where appropriate
- httpx
- websockets
- asyncio

Recommended project layout:

backend/
  app/
    api/
    core/
    auth/
    brokers/
    market_data/
    strategies/
    scanner/
    risk/
    oms/
    positions/
    paper_trading/
    backtesting/
    notifications/
    telegram/
    analytics/
    journal/
    scheduler/
    workers/
    models/
    schemas/
    services/
    repositories/
    monitoring/
    utils/

Do not place business logic inside FastAPI route handlers.

Routes should call services.

---

# 5. DATABASE

Use PostgreSQL.

Use Alembic for every schema change.

Never modify the production schema manually.

Create appropriate tables for at least:

- users
- roles
- permissions
- user_sessions
- broker_accounts
- broker_credentials
- broker_sessions
- broker_health
- instruments
- watchlists
- watchlist_items
- market_snapshots
- candles
- strategies
- strategy_versions
- strategy_parameters
- strategy_instances
- strategy_runs
- scanner_runs
- scanner_results
- signals
- trade_candidates
- orders
- order_events
- broker_orders
- executions
- positions
- position_events
- risk_profiles
- risk_limits
- risk_events
- daily_risk_state
- paper_orders
- paper_trades
- trades
- trade_journal
- backtests
- backtest_trades
- notifications
- telegram_events
- system_events
- audit_logs
- application_settings

Use:

- UUIDs where appropriate
- timezone-aware timestamps
- proper indexes
- unique constraints
- foreign keys
- PostgreSQL JSONB only where flexible metadata is genuinely required

Financial quantities must not use floating-point storage where precision matters.

Use appropriate Decimal/Numeric types.

---

# 6. REDIS

Use Redis for:

- real-time market cache
- live quotes
- pub/sub
- distributed locks
- strategy state
- order idempotency
- rate limiting
- short-lived broker-session information
- WebSocket coordination

Do not use Redis as the permanent source of truth.

PostgreSQL remains the durable source of truth.

---

# 7. BROKER ABSTRACTION

This is extremely important.

Trading strategies MUST NOT directly call Upstox or Firstock APIs.

Create a common broker interface similar to:

BrokerAdapter

Methods:

- authenticate()
- refresh_session()
- logout()
- get_profile()
- get_funds()
- get_holdings()
- get_positions()
- get_orders()
- get_order()
- place_order()
- modify_order()
- cancel_order()
- cancel_all_orders()
- exit_position()
- get_quote()
- get_historical_data()
- subscribe_market_data()
- subscribe_order_updates()
- health_check()

Implement:

UpstoxBrokerAdapter

FirstockBrokerAdapter

The rest of the application communicates only through the common broker interface.

---

# 8. NORMALIZED MODELS

Normalize broker-specific objects.

For example:

NormalizedOrder

Fields such as:

- internal_order_id
- broker_order_id
- broker
- account_id
- exchange
- symbol
- instrument_token
- transaction_side
- order_type
- product
- quantity
- filled_quantity
- pending_quantity
- price
- trigger_price
- average_price
- status
- rejection_reason
- submitted_at
- updated_at

Never expose raw broker response structures throughout the application.

Store raw broker responses separately for debugging/audit when useful.

---

# 9. MARKET DATA ENGINE

Create a dedicated market-data subsystem.

Architecture:

Broker WebSocket
        ↓
Broker Feed Parser
        ↓
Normalized Market Event
        ↓
Market Data Service
        ↓
Redis Pub/Sub
        ↓
Scanner / Strategies / Dashboard

Market data events should include:

- instrument
- LTP
- bid
- ask
- OHLC
- volume
- timestamp
- exchange timestamp
- feed timestamp

Implement stale-feed detection.

The strategy engine must never trade using stale data.

If feed becomes stale:

- stop new entries
- generate system alert
- mark market feed unhealthy

---

# 10. UPSTOX SUPPORT

Implement Upstox using its supported current API generation.

Support:

- authentication
- access-token lifecycle
- instrument master
- live market WebSocket
- order updates
- place order
- modify order
- cancel order
- order book
- trade book
- positions
- holdings
- funds
- historical candles
- broker health

Do not hardcode instrument keys.

Build an instrument-master synchronization process.

Upstox-specific logic must remain inside:

`brokers/upstox/`

---

# 11. FIRSTOCK SUPPORT

Implement Firstock independently.

Support:

- authentication
- TOTP/session handling as required
- order placement
- modification
- cancellation
- order history
- positions
- holdings
- market data WebSocket
- order notifications
- historical data where available

Firstock-specific implementation belongs only inside:

`brokers/firstock/`

Do not mix Firstock-specific field names into core trading logic.

---

# 12. MULTI-BROKER SAFETY

Never automatically resend the same trade to another broker after an ambiguous broker response.

Example:

Order submitted to Upstox.

Network timeout occurs.

DO NOT immediately submit the same trade to Firstock.

Instead mark:

`ORDER_STATUS_UNKNOWN`

Then run reconciliation:

1. Check broker order book.
2. Check trade book.
3. Search using internal correlation metadata if available.
4. Confirm execution state.
5. Only then determine next action.

Implement idempotency keys for internal order requests.

Duplicate-order protection is mandatory.

---

# 13. ORDER MANAGEMENT SYSTEM — OMS

Build a proper OMS.

Possible states:

- CREATED
- RISK_PENDING
- RISK_APPROVED
- RISK_REJECTED
- SUBMITTING
- SUBMITTED
- ACKNOWLEDGED
- OPEN
- PARTIALLY_FILLED
- FILLED
- MODIFY_PENDING
- CANCEL_PENDING
- CANCELLED
- REJECTED
- EXPIRED
- UNKNOWN
- RECONCILIATION_REQUIRED
- CLOSED

Every status transition must be recorded.

Create an immutable order event history.

---

# 14. RISK ENGINE

This module has authority over strategies.

Strategies generate intentions.

Risk engine decides whether execution is permitted.

Implement configurable limits:

- maximum risk per trade
- maximum rupees loss per trade
- maximum position value
- maximum portfolio exposure
- maximum symbol exposure
- maximum sector exposure
- maximum open positions
- maximum trades per day
- maximum daily realized loss
- maximum combined realized/unrealized daily loss
- maximum consecutive losses
- maximum order quantity
- maximum slippage
- minimum average volume
- minimum turnover/liquidity
- allowed trading hours
- no-new-entry cutoff time
- forced intraday exit time
- maximum orders per second

Risk responses:

- APPROVED
- REJECTED
- REDUCE_QUANTITY
- MANUAL_APPROVAL_REQUIRED

Every risk decision must contain a reason.

---

# 15. DAILY LOSS LOCK

Implement a hard daily loss lock.

Example:

Configured:

Maximum Daily Loss = ₹15,000

When:

Realized P&L + relevant open risk threshold <= -₹15,000

Then:

- prevent new positions
- disable automated entries
- notify dashboard
- send Telegram alert
- create audit event

Manual reset should require privileged access.

Do not silently reset during the trading session.

---

# 16. EMERGENCY KILL SWITCH

Create a global kill switch.

Dashboard options:

- Stop New Entries
- Disable All Strategies
- Cancel Pending Orders
- Cancel Selected Orders
- Exit Selected Positions
- Exit All Intraday Positions

Separate:

`Stop Trading`

from:

`Exit Everything`

Do not automatically liquidate positions merely because strategy execution is paused unless specifically configured.

---

# 17. STRATEGY ENGINE

Create a generic Strategy interface.

A strategy should receive normalized:

- market data
- candle data
- market context
- positions
- risk information

A strategy should return a SignalCandidate.

Example fields:

- strategy_id
- symbol
- direction
- setup
- score
- entry_type
- proposed_entry
- stop_loss
- targets
- confidence/score
- reason_codes
- indicator_snapshot
- timestamp

The strategy must NOT directly execute trades.

---

# 18. FIRST STRATEGY FRAMEWORK

Initially prepare the platform for a rules-based strategy using:

Market regime:

- NIFTY relative to 20 EMA

Trend filters:

- 50 SMA
- 150 SMA
- 200 SMA

Pullback conditions:

- price near 6 EMA
- price respecting 20 EMA

Breakout conditions:

- price structure
- volume confirmation

Additional filters:

- relative strength vs benchmark
- liquidity
- volume expansion
- trend alignment
- risk/reward

Keep parameters configurable.

Do not hard-code them deep inside Python classes.

---

# 19. SIGNAL SCORING

Implement configurable scoring.

Example:

Trend Alignment          20
EMA Structure            15
Relative Strength        20
Volume                   15
Breakout/Pullback        15
Market Regime            10
Risk Reward               5

Total                   100

Default interpretation:

90–100 = A+

80–89 = A

70–79 = Watch

Below 70 = Ignore

Store the complete scoring breakdown.

---

# 20. LIVE SCANNER

Build a high-performance scanner.

Initial universe:

- configurable watchlists
- NIFTY 50
- NIFTY 100
- NIFTY 200
- manually selected stocks

Scanner table fields:

- symbol
- LTP
- change %
- volume
- relative volume
- setup
- strategy
- score
- trend
- market regime
- proposed entry
- SL
- target
- risk/reward
- signal time

Support:

- sorting
- filtering
- saved filters
- search
- live updates

Selecting a scanner result should open a detailed signal page.

---

# 21. PAPER TRADING

Paper mode must use the exact same:

- strategy engine
- scanner
- risk engine

Only execution is simulated.

Simulate:

- market orders
- limit orders
- stop orders
- partial fills where reasonable
- slippage
- configurable brokerage/charges
- SL
- target
- trailing SL

Store all simulated trades permanently.

---

# 22. POSITION MANAGEMENT

Create Position Manager.

Show:

- broker
- symbol
- side
- quantity
- average price
- current price
- realized P&L
- unrealized P&L
- initial SL
- current SL
- targets
- R multiple
- strategy
- entry time
- duration

Actions:

- Exit
- Partial Exit
- Modify SL
- Move SL to Break-even
- Disable Trailing
- Enable Trailing

Automatic management options:

- break-even
- percentage trailing
- ATR trailing
- EMA trailing
- partial target
- time-based exit

---

# 23. TELEGRAM

Create Telegram Notification Service.

Telegram credentials must come from environment/configuration.

Never hardcode tokens.

Use Telegram initially for outbound notifications.

Signal message example:

A+ TRADE SETUP

HAL
EMA Pullback

Score: 93/100

Entry: ₹4,925
SL: ₹4,875
Target: ₹5,025

Risk: ₹5,000
R:R: 1:2

Market: BULLISH
Volume: 2.1x
Relative Strength: STRONG

Send alerts for:

- scanner signal
- trade approved
- order submitted
- order filled
- order rejected
- SL hit
- target hit
- position closed
- daily risk lock
- broker disconnected
- WebSocket disconnected
- feed stale
- system errors

Design the Telegram module so interactive APPROVE/REJECT buttons can be added later.

Do not implement insecure command execution through Telegram.

---

# 24. BACKTESTING ENGINE

Build a Backtest Lab.

Metrics:

- total trades
- winners
- losers
- win rate
- average winner
- average loser
- expectancy
- R multiple
- profit factor
- maximum drawdown
- Sharpe ratio
- Sortino ratio
- consecutive losses
- MFE
- MAE
- monthly P&L
- equity curve

Backtests must store:

- strategy version
- parameter version
- universe
- date range
- assumptions
- slippage
- charges

Results must be reproducible.

---

# 25. TRADING JOURNAL

Create an automatic journal.

Every live or paper trade should capture:

- trade ID
- strategy
- broker
- symbol
- setup
- signal time
- entry time
- entry price
- exit
- SL
- targets
- quantity
- risk
- P&L
- R multiple
- indicator snapshot
- score breakdown
- market regime
- order events
- slippage
- latency
- exit reason

Allow manual notes after the trade.

---

# 26. APPLICATION DASHBOARD

Create a premium institutional-style trading dashboard.

Not a generic admin template.

Sidebar:

COMMAND CENTER

MARKET
- Live Market
- Scanner
- Watchlists
- Signals

ALGO
- Strategies
- Strategy Builder
- Algo Monitor
- Scheduler

TRADING
- Order Book
- Positions
- Trades
- Portfolio

RISK
- Risk Center
- Limits
- Kill Switch

RESEARCH
- Backtesting
- Analytics
- Trading Journal

BROKERS
- Upstox
- Firstock

SYSTEM
- API Health
- WebSocket Health
- Logs
- Notifications
- Settings

---

# 27. COMMAND CENTER DESIGN

Show:

Market:

- NIFTY
- BANKNIFTY
- INDIA VIX
- market regime

Account:

- capital
- available funds
- exposure
- realized P&L
- unrealized P&L
- daily P&L

System:

- active strategies
- scanner health
- market-feed health
- Upstox status
- Firstock status
- Redis
- PostgreSQL
- worker health
- last data tick

Recent:

- latest signals
- current positions
- recent orders
- alerts

Provide a clean, premium, dense but readable interface.

---

# 28. BROKER HEALTH PAGE

For every broker show:

- authentication status
- account name
- account ID masked
- API connection
- WebSocket status
- last heartbeat
- last market tick
- order stream status
- latency
- funds
- positions count
- last successful request
- last error

Never expose:

- access tokens
- secrets
- TOTP secrets
- API secret

to the frontend.

---

# 29. AUTHENTICATION

Implement secure login.

Support:

- username/email
- password
- session management
- TOTP MFA
- password hashing using Argon2/bcrypt
- RBAC
- audit logs

Roles:

SUPER_ADMIN

TRADER

VIEWER

RISK_MANAGER

Optional future roles should be easy to add.

---

# 30. PERMISSIONS

Examples:

VIEWER:

- view dashboard
- view scanner
- view signals
- view journal

TRADER:

- approve trades
- modify permitted orders
- exit positions

RISK_MANAGER:

- modify risk controls
- trigger trading lock

SUPER_ADMIN:

- broker configuration
- user management
- system configuration
- emergency controls

Critical controls must be permission protected.

---

# 31. SECURITY

Broker secrets must be stored server-side.

Use environment variables or encrypted secret storage.

Never send:

- API secret
- access token
- refresh token
- TOTP secret
- database password

to the browser.

Also implement:

- CSRF protection where applicable
- secure cookies
- rate limiting
- request validation
- SQL-injection prevention
- XSS-safe rendering
- security headers
- protected WebSocket authentication
- audit trails

Sensitive values must be redacted from logs.

---

# 32. STATIC PUBLIC IP

The order execution architecture must ensure broker trading requests originate from the backend host associated with my Static Public IP.

Browser:

Browser
  ↓
Next.js
  ↓
FastAPI
  ↓
OMS
  ↓
Broker Adapter
  ↓
Static Public IP
  ↓
Broker API

Never place broker orders directly from frontend JavaScript.

Create a configuration page showing:

- detected outbound IP if safely available
- expected registered IP
- match/mismatch status

Do not expose unnecessary infrastructure details publicly.

---

# 33. WINDOWS DEVELOPMENT

The application will initially run on Windows.

Prefer:

- Windows 11
- WSL2 Ubuntu for backend/server tooling
- Docker Desktop if appropriate
- PostgreSQL
- Redis
- Git
- VS Code/Codex

Provide scripts for:

development startup

and eventually:

production startup

Possible commands:

docker compose up

or separated services depending on the architecture.

Do not require Kubernetes for the first deployment.

---

# 34. CONTAINERIZATION

Prepare:

- frontend Dockerfile
- backend Dockerfile
- worker Dockerfile if required
- docker-compose.yml

Services:

frontend

backend

worker

postgres

redis

nginx optional for local development

Use health checks.

Use persistent volumes for PostgreSQL.

---

# 35. LOGGING

Use structured logging.

Every trading-related log should include appropriate correlation identifiers such as:

- request_id
- signal_id
- internal_order_id
- broker_order_id
- strategy_id
- account_id

Never log secrets.

Maintain:

Application Log

Trading Log

Order/Event Log

Security Audit Log

Broker Integration Log

---

# 36. MONITORING

Build internal health endpoints:

/health

/health/database

/health/redis

/health/market-data

/health/brokers

/health/workers

Create a System Health page.

Future-ready for:

- Prometheus
- Grafana
- Sentry

---

# 37. FAIL-SAFE BEHAVIOUR

If critical services fail:

Market Data Failure:

Stop new automatic entries.

Broker Authentication Failure:

Stop broker order submissions.

Redis Failure:

Fail safely rather than guessing trading state.

Database Failure:

Prevent new automated trading if order state cannot be durably recorded.

Risk Engine Failure:

Reject new trades.

Unknown Order State:

Reconciliation required.

Never default to “continue trading” when trading state is uncertain.

---

# 38. RECONCILIATION ENGINE

Run periodic reconciliation between:

Internal OMS

and

Broker Order Book / Trade Book / Positions

Detect:

- missing broker order
- unknown internal order
- unmatched fill
- quantity mismatch
- position mismatch
- rejected order
- external/manual broker order

Display reconciliation warnings prominently.

Do not silently modify trading state.

---

# 39. SESSION START CHECK

Before enabling AUTO MODE each trading day run:

PRE-FLIGHT CHECK

Verify:

- database healthy
- Redis healthy
- broker authenticated
- market WebSocket connected
- order updates connected
- server date/time correct
- static-IP configuration valid
- risk configuration exists
- daily-loss state initialized
- instrument master current
- no unresolved orders
- no unresolved reconciliation issues
- kill switch not active

Only after successful pre-flight can automated strategies start.

---

# 40. MARKET CLOSE PROCESS

At end of session:

- disable new intraday entries
- reconcile orders
- reconcile positions
- calculate daily P&L
- close paper-trading session
- generate daily journal
- persist statistics
- send daily Telegram summary
- archive relevant metrics

Intraday positions should follow configured exit rules.

Do not automatically close delivery holdings.

---

# 41. CONFIGURATION

Use `.env.example`.

Never commit `.env`.

Example categories:

DATABASE

REDIS

APP

UPSTOX

FIRSTOCK

TELEGRAM

SECURITY

Create strongly typed configuration loading.

Fail startup if required production secrets are missing.

---

# 42. TESTING

Implement:

Unit tests

Integration tests

Broker mock tests

Strategy tests

Risk engine tests

OMS state transition tests

Paper-trading tests

Reconciliation tests

API tests

Critical trading modules require good test coverage.

Never test real order placement automatically.

Use broker mocks/sandbox equivalents whenever possible.

---

# 43. DATABASE SEEDING

Seed only useful base configuration:

- roles
- permissions
- default application settings

Do not create fake production trading history.

Demo data should be clearly isolated from production.

---

# 44. UI SAFETY

Use highly visible mode indicators.

Example:

SCANNER

PAPER

ASSISTED

LIVE AUTO

LIVE AUTO must be visually obvious.

Before changing from PAPER/ASSISTED to LIVE AUTO:

require deliberate user confirmation.

Do not make accidental live trading easy.

---

# 45. LIVE ORDER CONFIRMATION

For ASSISTED MODE show:

Symbol

Side

Quantity

Order Type

Product

Entry

Stop Loss

Target

Estimated Risk

Broker

Strategy

Then require:

CONFIRM TRADE

Do not execute because the user merely opened the dialog.

---

# 46. DO NOT USE AI FOR DIRECT EXECUTION

An AI/LLM module may later:

- explain signals
- summarize market conditions
- review journal
- analyze losing trades
- explain risk rejection

AI must not bypass deterministic strategy and risk systems.

An LLM must never directly send raw broker orders.

---

# 47. DEVELOPMENT PHASES

Implement using these phases.

## PHASE 0 — FOUNDATION

Create:

- monorepo/project structure
- Next.js frontend
- FastAPI backend
- PostgreSQL
- Redis
- authentication
- Docker/dev environment
- configuration
- migrations
- logging
- health checks

Do not start automated trading yet.

## PHASE 1 — BROKER FOUNDATION

Implement:

- common BrokerAdapter
- Upstox adapter
- Firstock adapter
- broker settings UI
- authentication
- profile
- funds
- holdings
- positions
- orders
- health checks

## PHASE 2 — LIVE MARKET DATA

Implement:

- broker WebSockets
- normalized feed
- Redis
- quote service
- market-data health
- instrument master
- live dashboard

## PHASE 3 — SCANNER

Implement:

- indicators
- market regime
- scanning engine
- scoring engine
- watchlists
- scanner UI
- signal details

NO automatic orders.

## PHASE 4 — TELEGRAM

Implement:

- signal notifications
- system alerts
- risk alerts
- broker alerts

## PHASE 5 — PAPER TRADING

Implement:

- simulated OMS
- simulated positions
- fills
- SL
- targets
- trailing
- journal

Validate strategy behavior before live trading.

## PHASE 6 — RISK ENGINE

Implement:

- position sizing
- risk limits
- daily-loss lock
- rate limiting
- duplicate protection
- kill switch
- pre-flight checks

## PHASE 7 — ASSISTED LIVE EXECUTION

Implement:

Signal

→ Risk Engine

→ User confirmation

→ OMS

→ Broker Adapter

→ Broker

Add reconciliation.

## PHASE 8 — AUTO EXECUTION

Only after all previous modules work reliably.

AUTO MODE remains disabled until explicitly enabled.

## PHASE 9 — BACKTESTING

Implement historical-data engine and analytics.

## PHASE 10 — ADVANCED ANALYTICS

Implement:

- advanced journal
- performance attribution
- strategy comparison
- AI assistant

---

# 48. FIRST PRODUCTION MILESTONE

The first important usable version should provide:

Upstox + Firstock authentication

↓

Live Market Feed

↓

NSE Scanner

↓

Strategy Scoring

↓

Signal Dashboard

↓

Telegram Signal

↓

Paper Trading

This is the first target.

Do NOT jump straight to fully automatic live order placement.

---

# 49. CODING QUALITY

Follow:

- SOLID principles
- separation of concerns
- dependency injection where useful
- typed Python
- strict TypeScript
- reusable components
- clean APIs
- documented models
- service/repository architecture where justified
- minimal duplication

Avoid giant files.

Avoid giant React components.

Avoid giant FastAPI route modules.

Avoid God classes.

---

# 50. DOCUMENTATION

Create:

README.md

docs/
  ARCHITECTURE.md
  DEVELOPMENT.md
  DEPLOYMENT.md
  BROKER_INTEGRATION.md
  MARKET_DATA.md
  OMS.md
  RISK_ENGINE.md
  STRATEGY_ENGINE.md
  PAPER_TRADING.md
  SECURITY.md
  DATABASE.md
  API.md
  TROUBLESHOOTING.md
  IMPLEMENTATION_STATUS.md

Create