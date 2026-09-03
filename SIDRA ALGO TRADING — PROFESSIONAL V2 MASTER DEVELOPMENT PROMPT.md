# SIDRA ALGO TRADING — PROFESSIONAL V2 MASTER DEVELOPMENT PROMPT

You are working as the principal software architect, senior quantitative trading systems engineer, senior full-stack engineer, DevSecOps engineer, UI/UX engineer, database architect, and QA engineer for an existing project.

## PROJECT

Repository:

`https://github.com/Shamseer1988/sidra-algo-trading.git`

Default branch:

`main`

Product name:

**Sidra Algo Trading**

The application already contains a working foundation including:

- Next.js / React frontend
- FastAPI backend
- PostgreSQL
- Redis
- authentication
- scanner
- paper strategy
- Upstox integration
- Firstock integration
- Telegram integration
- market data handling
- worker services
- paper signals
- trading controls
- safety controls
- audit-related components
- Docker
- Alembic
- automated tests
- GitHub Actions

Your task is NOT to recreate the project from scratch.

You must carefully audit, preserve, improve, refactor, extend, test, and production-harden the existing application.

---

# PRIMARY OBJECTIVE

Transform the existing project into a professional, premium-quality, institutional-style automated algorithmic trading platform with:

- premium trading-terminal UI
- Light Mode
- Dark Mode
- System Theme
- responsive desktop design
- real-time market data
- professional scanner
- strategy engine
- paper trading
- backtesting
- shadow trading
- assisted trading
- fully automated trading
- Upstox execution
- Firstock execution architecture
- advanced Risk Engine
- professional Order Management System
- Position Management
- P&L
- reconciliation
- journal
- analytics
- Telegram automation
- monitoring
- audit logs
- security
- recovery mechanisms
- production deployment suitable for a home PC using a static public IP

This application handles financial trading.

Therefore:

**Safety, deterministic behavior, data integrity, idempotency, risk management, auditability, and failure recovery are more important than speed of development.**

---

# CRITICAL WORKING RULE

## AUTO-CONTINUE THROUGH ALL PHASES

You must execute this project phase by phase.

After completing a phase:

1. run all applicable tests
2. fix failures
3. review the implementation
4. update documentation
5. commit logically grouped changes if Git access is available
6. immediately continue to the next phase

### DO NOT WAIT FOR MY PERMISSION BETWEEN PHASES.

Do not stop and ask:

- "Should I continue?"
- "Would you like me to proceed?"
- "Can I start Phase 2?"
- "Please confirm before continuing."

Continue automatically.

Only stop when an unavoidable external dependency requires human action, for example:

- broker OAuth login
- API key creation
- static IP registration
- Telegram bot token
- broker account approval
- CAPTCHA
- exchange/broker approval
- a secret that does not exist locally

Even in those cases:

1. complete every task that does not require the missing external dependency
2. provide the exact manual action required
3. continue with all other independent work

Never fabricate credentials.

Never put secrets into source control.

---

# VERY IMPORTANT — PRESERVE EXISTING WORK

Before modifying anything:

- inspect the entire repository
- understand the current architecture
- inspect Git status
- inspect Git history
- inspect environment configuration
- inspect Docker configuration
- inspect migrations
- inspect tests
- inspect API routes
- inspect workers
- inspect Redis usage
- inspect frontend architecture
- inspect database models
- inspect broker integrations
- inspect Telegram integration
- inspect scanner strategy
- inspect current security model

Do not blindly rewrite existing working modules.

Prefer incremental refactoring.

Maintain backwards compatibility where practical.

Do not remove existing working functionality unless the replacement is demonstrably better and tested.

---

# FIRST STEP — FULL CODEBASE AUDIT

Perform a complete repository audit before implementing new features.

Produce an internal implementation checklist covering:

## Architecture

Review:

- frontend architecture
- backend architecture
- database
- Redis
- async workers
- WebSockets
- Docker
- networking
- broker integration
- authentication
- scanner
- paper trading
- Telegram
- deployment

## Code Quality

Identify:

- duplicate logic
- giant components
- dead code
- inconsistent naming
- missing typing
- unsafe exception handling
- blocking calls
- async misuse
- race conditions
- database transaction risks
- missing indexes
- missing constraints
- performance bottlenecks
- inadequate abstractions

## Security

Audit:

- authentication
- JWT
- refresh tokens
- HttpOnly cookies
- SameSite
- Secure cookies
- CSRF
- CORS
- rate limiting
- brute-force protection
- password hashing
- session revocation
- RBAC
- secrets
- audit logs
- API exposure
- WebSocket authentication
- Redis
- PostgreSQL exposure
- Docker permissions
- dependency vulnerabilities

## Trading Safety

Audit:

- duplicated orders
- retry behavior
- risk calculations
- stale market data
- partial fills
- order state transitions
- order reconciliation
- exchange/broker disconnects
- worker crashes
- market-session checks
- trading calendar
- daily-loss limits
- emergency stop
- strategy isolation
- execution idempotency

---

# KNOWN ISSUES TO VERIFY AND FIX

The existing project previously had these issues.

Verify whether they still exist. If they do, fix them.

## 1. GitHub Actions Branch

The repository now uses:

`main`

Ensure all GitHub Actions workflows correctly trigger for:

- `main`
- pull requests targeting `main`

Remove obsolete `master` references.

---

## 2. Upstox Timestamp Handling

Do not bucket candles using only local message receipt time.

Maintain:

- `exchange_timestamp`
- `received_timestamp`

Use exchange/last-trade timestamp for market candle assignment.

Track latency separately.

---

## 3. Strategy Configuration

The scanner must NOT ignore settings stored by the Strategies UI.

Create a professional strategy architecture such as:

`StrategyRegistry`

Each strategy must support:

- enabled/disabled
- version
- parameters
- symbols/universe
- session
- risk settings
- filters
- maximum signals
- cooldown
- score threshold

The worker must dynamically consume persisted enabled strategy configurations.

---

## 4. Authentication Refresh

The frontend must automatically handle an expired access token.

Implement:

1. API request
2. receive 401
3. call refresh endpoint once
4. retry original request
5. redirect to login only if refresh fails

Prevent refresh-request storms using a single-flight refresh mechanism.

Do not redirect the user to login simply because an unrelated API returns HTTP 500.

---

## 5. Telegram Configuration

Create one authoritative Telegram configuration service.

Environment and encrypted database configuration must behave consistently.

Status, webhook, sendMessage, callback processing and scanner alerts must all use the same resolved configuration.

---

## 6. Public API Exposure

Production FastAPI must not be publicly exposed as:

`0.0.0.0:8000`

Use an HTTPS reverse proxy.

Only expose ports:

- 80 where necessary for redirect/certificate
- 443

Keep:

- FastAPI
- PostgreSQL
- Redis

private/internal.

---

## 7. WebSocket

Do not hardcode:

`:8000`

in browser WebSocket URLs.

Use same-origin secure WebSockets:

`wss://<public-domain>/api/...`

or an appropriate `/ws/...` route.

Reverse proxy WebSocket upgrades correctly.

---

## 8. Docker Hardening

Production containers should preferably:

- run as non-root
- use minimal images
- have health checks
- use restart policies
- drop unnecessary capabilities
- use no-new-privileges where appropriate
- use resource limits where appropriate
- have log rotation
- avoid exposing internal ports publicly

---

## 9. Trading Calendar

Implement a Trading Calendar service.

It must distinguish:

- NSE normal trading days
- weekends
- holidays
- special trading sessions
- market open
- market closed
- pre-open
- post-market

Never rely solely on:

`09:15–15:30`

time comparisons.

---

## 10. Data Quality

Before a strategy can generate a trade:

validate market-data quality.

Track:

- expected bars
- received bars
- missing buckets
- stale ticks
- feed latency
- duplicate ticks
- out-of-order ticks

Possible states:

- GOOD
- DEGRADED
- STALE
- INVALID

Block automated execution on INVALID or STALE data.

---

# TARGET TECHNOLOGY STACK

Retain the existing stack where appropriate.

## Frontend

Use:

- Next.js current stable supported version
- React
- TypeScript strict mode
- Tailwind CSS
- shadcn/ui
- Radix primitives where useful
- TanStack Query
- Zustand only for appropriate client/global state
- React Hook Form
- Zod
- Lucide icons
- TradingView Lightweight Charts
- Recharts only for non-market analytics if appropriate
- WebSockets / Server-Sent Events where appropriate

Do not use excessive animation.

Animations should be subtle, fast, professional and useful.

---

# DESIGN SYSTEM

Create a premium professional design system.

The application must support:

- Light
- Dark
- System

Use semantic design tokens instead of hardcoded colors everywhere.

Example semantic tokens:

- background
- foreground
- card
- card-muted
- border
- primary
- secondary
- accent
- positive
- negative
- warning
- info
- destructive

Trading states must be visually clear.

Use premium typography.

Recommended:

- Geist
- Geist Mono for numeric/market data

Use tabular numerals for:

- price
- P&L
- percentage
- quantity
- market statistics

---

# UI STYLE

The application should look like a premium professional trading terminal.

References in spirit:

- Bloomberg-style information density
- TradingView clarity
- Zerodha Kite simplicity
- Upstox modern UI
- professional institutional OMS dashboards

Do NOT directly clone any product.

Create Sidra's own visual identity.

Use:

- clean grid
- restrained shadows
- elegant borders
- compact professional cards
- precise spacing
- clear numerical hierarchy
- consistent status badges
- responsive layouts
- premium data tables

Avoid:

- cartoon UI
- excessive gradients
- overly rounded cards
- oversized whitespace
- giant marketing-style dashboard cards

This is a trading application, not a marketing website.

---

# SIDEBAR ARCHITECTURE

Create approximately this structure:

SIDRA ALGO

### TRADING

- Overview
- Market
- Scanner
- Signals
- Strategies
- Orders
- Positions

### RISK & ANALYTICS

- Risk Center
- Performance
- Backtesting
- Journal

### BROKERS

- Upstox
- Firstock

### AUTOMATION

- Automation Rules
- Scheduler
- Telegram

### SYSTEM

- System Health
- Audit Log
- Users
- Settings

Sidebar must support:

- full
- collapsed icon mode
- responsive drawer
- tooltips
- active navigation state

---

# GLOBAL HEADER

Header should contain:

- market state
- selected broker
- scanner state
- execution mode
- current IST market clock
- alert indicator
- connection status
- theme selector
- user menu

Important execution-mode badges:

- REPLAY
- BACKTEST
- PAPER
- SHADOW
- ASSISTED
- LIVE

LIVE must be visually unmistakable.

---

# OVERVIEW DASHBOARD

Create a professional command center.

## Market Strip

Show examples:

- NIFTY 50
- BANK NIFTY
- FIN NIFTY where available
- INDIA VIX
- market regime

Display:

- price
- change
- percentage
- trend

---

## System Status

Show separate health indicators:

- Market Feed
- Broker
- Scanner
- Strategy Engine
- Risk Engine
- Execution Engine
- Database
- Redis
- Telegram

States:

- ONLINE
- CONNECTING
- DEGRADED
- OFFLINE
- HALTED

---

## Trading Metrics

Show:

- account capital
- available capital
- realized P&L
- unrealized P&L
- net P&L
- daily risk used
- open risk
- trades today
- win rate
- open positions
- max drawdown

---

## Dashboard Panels

Include:

- active signals
- open positions
- today's orders
- scanner activity
- strategy performance
- recent risk events
- equity curve
- broker latency
- market-data latency
- recent alerts
- system events

---

# MARKET PAGE

Create a market workspace containing:

- index watch
- watchlists
- top gainers
- top losers
- high volume
- relative volume
- market breadth
- sector strength
- relative strength
- volatility
- scanner candidates

Charts should support:

- candlesticks
- volume
- VWAP
- EMA
- ORB
- entry
- stop
- target
- trade markers

Use TradingView Lightweight Charts.

---

# SCANNER

The Scanner must become a professional real-time workspace.

Table columns should include:

- Symbol
- LTP
- Change %
- Volume
- RVOL
- VWAP
- EMA
- Relative Strength
- Market Regime
- Setup
- Direction
- Score
- Data Quality
- Last Update
- Action

Features:

- column sorting
- filtering
- searching
- column visibility
- saved views
- pagination/virtualization where required
- row highlighting
- WebSocket updates

Clicking a symbol opens a detailed setup panel.

Display:

- chart
- strategy
- score
- score breakdown
- reasons
- failed conditions
- breakout
- retest
- volume confirmation
- VWAP
- EMA
- market confirmation
- relative strength
- proposed entry
- stop
- target
- quantity
- risk
- R:R

Do not show only accepted signals.

Also record rejected setups and explain why they were rejected.

This is essential for debugging strategies.

---

# STRATEGY ENGINE

Build a modular strategy framework.

Base strategy interface should conceptually support:

- metadata
- name
- version
- enabled state
- universe
- parameters
- prerequisites
- evaluate()
- generate_signal()
- risk model
- explanation
- deterministic replay

Existing ORB Retest should be migrated into this architecture without changing its expected behavior unintentionally.

Example future strategies:

- ORB Retest
- VWAP Pullback
- EMA Momentum
- Breakout + Volume
- Relative Strength Pullback

Do NOT enable new live strategies without tests.

---

# STRATEGY BUILDER / SETTINGS

Allow ADMIN/TRADER to configure strategy parameters.

Examples:

- enable/disable
- symbols
- minimum score
- opening range duration
- RVOL threshold
- VWAP confirmation
- EMA conditions
- minimum R:R
- risk per trade
- max trades/day
- cooldown
- allowed sessions
- long/short
- index confirmation
- sector confirmation

Version strategy configuration.

Each signal must retain the exact configuration version that created it.

---

# SIGNAL MODEL

Signal states should support:

- DETECTED
- FILTERED
- QUALIFIED
- RISK_REJECTED
- READY
- APPROVAL_PENDING
- APPROVED
- EXECUTING
- EXECUTED
- EXPIRED
- CANCELLED
- FAILED

Store:

- strategy
- strategy version
- symbol
- exchange
- side
- timestamp
- market conditions
- entry
- stop
- target
- score
- score components
- risk
- quantity
- reason
- rejection reason
- source data references

---

# EXECUTION ARCHITECTURE

This is critical.

A strategy must NEVER directly call a broker order API.

Use this flow:

Market Data

→ Strategy Engine

→ Signal

→ Risk Engine

→ OrderIntent

→ Order Manager

→ Execution Gateway

→ Broker Adapter

→ Broker Order

→ Fill

→ Position

→ Reconciliation

---

# ORDER INTENT

Create an immutable OrderIntent representing the decision to trade.

Example fields:

- id
- idempotency_key
- signal_id
- strategy_id
- symbol
- exchange
- side
- order_type
- quantity
- price
- trigger_price
- stop
- target
- risk_amount
- execution_mode
- created_at
- approved_at
- risk_snapshot
- strategy_snapshot

---

# IDEMPOTENCY

Every execution must have a unique idempotency key.

Example:

`SIDRA-20260901-RELIANCE-ORB-LONG-001`

Create a database unique constraint.

Never submit a duplicate order because a request timed out.

---

# ORDER STATE MACHINE

Implement explicit deterministic states:

- CREATED
- RISK_PENDING
- RISK_APPROVED
- RISK_REJECTED
- SUBMITTING
- SUBMITTED
- ACKNOWLEDGED
- PARTIALLY_FILLED
- FILLED
- CANCEL_PENDING
- CANCELLED
- REJECTED
- EXPIRED
- UNKNOWN

`UNKNOWN` is extremely important.

Example:

Sidra sends an order.

Network timeout occurs.

Sidra does NOT know whether broker accepted it.

Never blindly resend.

Move to:

`UNKNOWN`

Then reconcile against broker orderbook.

---

# BROKER ADAPTER

Create a common broker interface.

Conceptually:

- authenticate
- get_profile
- get_funds
- get_margin
- get_positions
- get_holdings
- get_orders
- get_order
- place_order
- modify_order
- cancel_order
- get_trades
- subscribe_market_data
- unsubscribe_market_data

Implement adapters:

- UpstoxAdapter
- FirstockAdapter

Broker-specific logic must stay inside adapters.

Core strategy/risk/order systems must remain broker-independent.

---

# UPSTOX AUTHENTICATION

Do NOT automate browser entry of:

- mobile number
- PIN
- TOTP secret

Remove unsupported Playwright-based credential automation if still present.

Use supported broker authorization mechanisms.

Keep credentials server-side.

Encrypt stored tokens.

Maintain token expiry awareness.

Provide clear admin UI for:

- connection status
- token expiry
- authorization required
- reconnect
- static IP
- account
- market data status
- order API status

---

# FIRSTOCK

Build Firstock within the same BrokerAdapter architecture.

Maintain broker-independent core trading logic.

Allow the user to choose:

- Market Data Broker
- Execution Broker

Do not assume these must always be identical.

---

# RISK ENGINE

This is one of the most important components.

Every order must pass Risk Engine approval immediately before submission.

Implement:

## Account Controls

- account capital
- available margin
- risk per trade
- max daily loss
- max weekly loss
- max drawdown
- max open risk

## Position Controls

- max open positions
- max symbol exposure
- max sector exposure
- max correlated exposure

## Trading Controls

- max trades/day
- max orders/minute
- max consecutive losses
- cooldown after loss
- max strategy exposure

## Market Controls

- stale price protection
- spread protection
- slippage protection
- liquidity
- price-band validation
- circuit-limit awareness where available
- abnormal volatility

## Infrastructure Controls

- feed health
- market-data age
- broker latency
- broker connectivity
- Redis health
- DB health
- execution worker health
- duplicate-order protection

## Session Controls

- trading day
- market session
- broker session
- static IP status where applicable
- execution mode
- emergency-stop state

Any critical risk failure means:

`ORDER BLOCKED`

not merely a warning.

---

# RISK RESERVATION

Prevent two simultaneous strategies from spending the same risk allowance.

Create transactional RiskReservation records.

Reserve risk before order submission.

Release or convert reservations when orders:

- fill
- reject
- expire
- cancel

This must be race-condition safe.

---

# EMERGENCY STOP

Emergency stop is a system-level safety mechanism.

When activated:

1. stop new signals from being executable
2. block new order intents
3. stop new broker submissions
4. cancel pending entry orders where configured
5. optionally flatten open positions depending on administrator setting
6. disable strategies
7. record an immutable audit event
8. send Telegram critical alert
9. require an ADMIN re-arm action

Do not automatically re-enable after restart.

Emergency stop should survive process restart using PostgreSQL/Redis state.

---

# EXECUTION MODES

Implement explicit modes:

## REPLAY

Historical candle replay.

No broker activity.

## BACKTEST

Historical strategy simulation.

No broker activity.

## PAPER

Live market data.

Simulated orders.

No real broker orders.

## SHADOW

Live market + real strategies + real risk checks.

Generate exactly what WOULD have been sent.

Do NOT place order.

Record shadow order.

## ASSISTED

Signal

→ Risk

→ Approval

→ Broker execution

Approval can come from:

- web UI
- Telegram callback

Re-run Risk Engine immediately before actual submission.

## LIVE

Fully automatic:

Signal

→ Risk

→ OrderIntent

→ Broker

LIVE must remain disabled until all safety gates are satisfied.

---

# LIVE ACTIVATION GATES

LIVE mode must require explicit ADMIN activation.

Require all mandatory checks:

- broker connected
- API credentials valid
- account identified
- static IP status valid where required
- market feed healthy
- trading calendar valid
- database healthy
- Redis healthy
- execution worker healthy
- Risk Engine healthy
- no emergency stop
- strategy validated
- reconciliation successful
- account positions synchronized
- no unknown orders
- current session allowed

Display the checklist to the administrator.

LIVE must not activate silently.

---

# PAPER ORDER ENGINE

Before real orders, build a realistic paper execution engine.

Support:

- MARKET
- LIMIT
- STOP
- STOP-LIMIT where appropriate
- partial fills
- rejected orders
- cancelled orders
- expiry
- slippage
- spread
- latency simulation
- brokerage/fees
- position average price
- realized P&L
- unrealized P&L
- stop
- target

Paper trading should behave as similarly as reasonably possible to the real execution architecture.

---

# TRANSACTION COST MODEL

For Indian equity trading, implement configurable cost components.

Do not hardcode assumptions permanently.

Support configurable:

- brokerage
- STT
- exchange transaction charges
- GST
- SEBI charges
- stamp duty
- slippage

Keep rates configurable because regulations and broker pricing can change.

---

# POSITION MANAGEMENT

Create professional position tracking.

Position states:

- OPENING
- OPEN
- REDUCING
- CLOSING
- CLOSED
- RECONCILIATION_REQUIRED

Track:

- symbol
- strategy
- side
- quantity
- average entry
- CMP
- stop
- target
- realized P&L
- unrealized P&L
- total P&L
- risk
- R multiple
- opened at
- closed at
- broker
- broker position ID/reference

---

# ORDER MANAGEMENT UI

Create professional Orders page.

Tabs:

- Active
- Completed
- Rejected
- Cancelled
- Unknown

Columns:

- Time
- Symbol
- Side
- Type
- Qty
- Price
- Trigger
- Filled
- Status
- Broker
- Strategy
- Signal
- Mode
- Latency

Order detail drawer must show full lifecycle.

Example:

Signal generated  
Risk approved  
Intent created  
Sent to broker  
Broker acknowledged  
Partial fill  
Fill complete  
Position opened

Show timestamps for every transition.

---

# RECONCILIATION ENGINE

Continuously compare Sidra state against broker state.

Detect:

- UNKNOWN_ORDER
- MISSING_ORDER
- UNEXPECTED_ORDER
- UNEXPECTED_POSITION
- QUANTITY_MISMATCH
- PRICE_MISMATCH
- MISSING_FILL
- STALE_POSITION

On serious reconciliation failures:

- block new automated orders
- set execution state DEGRADED or HALTED
- create audit event
- send critical Telegram alert

---

# BACKTEST ENGINE

Implement a proper backtesting system.

Allow:

- symbol selection
- index universe
- date range
- strategy
- strategy version
- capital
- risk %
- fees
- slippage

Results:

- total trades
- wins
- losses
- win rate
- gross profit
- gross loss
- net P&L
- profit factor
- expectancy
- average R
- max drawdown
- max consecutive losses
- Sharpe where meaningful
- recovery factor
- average holding time

Charts:

- equity curve
- drawdown
- monthly returns
- trade distribution
- R distribution

Provide individual backtest trades.

Prevent look-ahead bias.

Use only information available at the simulated timestamp.

---

# PERFORMANCE ANALYTICS

Create professional analytics.

Filters:

- date
- strategy
- broker
- symbol
- direction
- execution mode

Display:

- P&L
- win rate
- expectancy
- profit factor
- average winner
- average loser
- R multiple
- drawdown
- best day
- worst day
- long vs short
- strategy comparison
- symbol performance
- weekday performance
- hourly performance

---

# JOURNAL

Automatically journal every trade.

Store:

- strategy
- signal
- setup
- entry
- exit
- stop
- target
- P&L
- R
- fees
- execution quality
- slippage
- market context
- screenshots/chart snapshot metadata where appropriate
- notes
- tags

Allow manual notes after trades.

---

# AUTOMATION RULES

Create an Automation section.

Rules could include:

- strategies allowed
- market sessions
- execution mode
- max trades
- daily stop
- weekly stop
- strategy schedule
- broker fallback behavior

Rule execution must be logged.

---

# SCHEDULER

Create market-aware scheduling.

Examples:

- system pre-market initialization
- instrument refresh
- broker connection check
- scanner start
- scanner stop
- daily reconciliation
- daily P&L report
- database maintenance
- Telegram summary

Use Asia/Kolkata for Indian-market scheduling.

Persist database timestamps in UTC.

---

# TELEGRAM

Telegram should support:

- signal alerts
- order alerts
- fill alerts
- risk rejection
- emergency stop
- system health
- broker disconnect
- reconciliation failure
- daily summary

For assisted trading:

Buttons:

- APPROVE
- REJECT

Never trust Telegram approval alone.

After approval:

run Risk Engine again.

Validate:

- approver
- callback authenticity
- signal status
- expiry
- current market price
- current risk
- data freshness

Then create executable order intent.

---

# TELEGRAM SECURITY

Use:

- Telegram secret token validation
- allowed user IDs
- allowed chat ID
- callback deduplication
- request/event audit logs
- idempotency

Never accept execution commands from unauthorized users.

---

# USER MANAGEMENT

Roles:

## ADMIN

Full system control.

## TRADER

Trading operational access according to privileges.

## VIEWER

Read-only.

Implement proper backend RBAC.

Never rely solely on hiding frontend buttons.

---

# AUDIT LOG

Trading systems require detailed auditability.

Record:

- login
- logout
- failed login
- settings changes
- strategy changes
- risk settings changes
- broker configuration
- authorization events
- signal creation
- approval
- rejection
- order submission
- modification
- cancellation
- fill
- position changes
- emergency stop
- live activation
- reconciliation
- administrative actions

Prefer structured data:

- event_type
- actor
- resource
- resource_id
- previous_state
- new_state
- metadata
- IP
- timestamp

Sensitive secrets must never be logged.

---

# SYSTEM HEALTH

Create a dedicated professional System Health page.

Monitor:

- API
- web
- PostgreSQL
- Redis
- worker
- scanner
- market feed
- Upstox
- Firstock
- Telegram
- execution gateway
- reconciliation
- disk
- memory
- CPU

Display:

- current state
- last heartbeat
- latency
- errors
- reconnect count

---

# OBSERVABILITY

Implement structured logging.

Use correlation IDs.

Important identifiers:

- request_id
- signal_id
- order_intent_id
- order_id
- position_id
- strategy_run_id

Ensure errors can be traced from:

Strategy → Signal → Risk → Order → Fill → Position.

---

# DATABASE ARCHITECTURE

Review existing tables and extend carefully.

Likely entities include:

- users
- sessions
- login_history
- audit_logs
- application_settings
- broker_credentials
- broker_sessions
- instruments
- market_candles
- market_indicator_snapshots
- strategy_definitions
- strategy_versions
- strategy_runs
- signals
- signal_evaluations
- order_intents
- risk_reservations
- risk_events
- broker_orders
- order_events
- fills
- positions
- position_events
- broker_reconciliations
- paper_orders
- paper_fills
- backtest_runs
- backtest_trades
- journal_entries
- telegram_alerts
- telegram_events
- system_events

Use proper:

- foreign keys
- indexes
- unique constraints
- timestamps
- status enums or constrained values
- transaction boundaries

Generate Alembic migrations.

Never modify production schema manually without migration.

---

# FRONTEND REFACTOR

The current frontend must not remain centered around one giant `app-shell.tsx`.

Refactor into a maintainable feature-based architecture.

Example:

```text
apps/web/
  app/
  components/
    ui/
    layout/
    charts/
    trading/
  features/
    dashboard/
    market/
    scanner/
    signals/
    strategies/
    orders/
    positions/
    risk/
    performance/
    backtest/
    journal/
    brokers/
    telegram/
    system/
  hooks/
  lib/
    api/
    auth/
    websocket/
    formatting/
    validation/
  stores/
  types/
```

Do not over-engineer.

Use TanStack Query for server state.

Use Zustand only when suitable for:

- UI preferences
- selected workspace state
- transient trading terminal state

Avoid duplicating server state inside Zustand.

---

# API CLIENT

Create a robust typed API layer.

Features:

- typed responses
- base error handling
- automatic auth refresh
- CSRF
- timeout
- retry only where safe
- request cancellation
- normalized errors

Never automatically retry order-submission requests unless the idempotency architecture makes it safe.

---

# REAL-TIME DATA

Build a dedicated real-time client layer.

Support:

- connection status
- reconnect
- exponential backoff
- heartbeat
- stale-data detection
- message sequencing where useful
- subscription lifecycle

Do not create separate unmanaged WebSockets in random components.

---

# RESPONSIVENESS

Primary target:

desktop professional trading workstation.

Also support:

- laptop
- tablet
- basic mobile monitoring

Do not sacrifice desktop information density for mobile-first design.

On mobile, trading execution actions should be deliberately designed to reduce accidental activation.

---

# SECURITY REQUIREMENTS

Implement or verify:

- Argon2id passwords
- HttpOnly session cookies
- Secure cookies in production
- SameSite
- CSRF
- CORS allowlist
- refresh-token rotation
- server-side sessions
- account lockout
- login rate limiting
- RBAC
- security headers
- audit logging
- encrypted broker tokens
- Fernet/key-management abstraction
- no secrets in browser
- no secrets in Git
- safe error messages
- dependency scanning
- non-root containers
- internal Redis/Postgres
- HTTPS only in production

Consider adding:

- Content-Security-Policy
- HSTS
- trusted proxy configuration
- request size limits
- API rate limiting where appropriate

---

# STATIC IP / HOME PC DEPLOYMENT

The system will eventually run on a Windows home PC with a static public IP.

Design production deployment carefully.

Preferred architecture:

Internet

→ Router / Firewall

→ HTTPS Reverse Proxy

→ Sidra Web

→ Sidra API internally

→ PostgreSQL internally

→ Redis internally

Never expose PostgreSQL publicly.

Never expose Redis publicly.

Never expose FastAPI port 8000 publicly.

Use Windows/WSL2/Docker based deployment as appropriate.

Document:

- firewall rules
- reverse proxy
- TLS
- static IP
- domain
- backup
- restart
- monitoring

---

# BROKER AND EXCHANGE COMPLIANCE

Before enabling real order execution:

validate the current Upstox, Firstock, NSE and applicable SEBI requirements.

Do not rely permanently on old assumptions.

Build configuration fields for compliance-related items such as:

- registered static IP
- broker API status
- approved algo name if applicable
- required headers
- broker session
- API expiry
- account mapping

Create a Compliance Status panel.

Example:

- API Key: VALID
- Static IP: VERIFIED
- Current outbound IP: MATCH
- Broker Session: CONNECTED
- Algo Registration: VALID / NOT REQUIRED
- Algo Name: configured
- Market Session: OPEN
- Execution Permission: READY

If compliance requirements are uncertain:

block automatic LIVE activation and display actionable status.

---

# ENVIRONMENT CONFIGURATION

Update `.env.example`.

Clearly separate:

- development
- test
- paper
- production

Never put real values inside `.env.example`.

Organize config sections:

- application
- database
- Redis
- auth
- encryption
- Upstox
- Firstock
- Telegram
- trading
- market data
- observability

---

# PRODUCTION CONFIG VALIDATION

At startup in production:

refuse to start or refuse LIVE execution when unsafe defaults are present.

Examples:

- default JWT secret
- missing encryption key
- insecure cookie configuration
- debug mode
- unknown broker
- invalid database
- unsupported execution mode

---

# TESTING

This application requires extensive testing.

## Backend Unit Tests

Test:

- strategy
- risk
- sizing
- fees
- state machines
- market calculations
- calendar
- idempotency
- auth
- permissions

## Integration Tests

Test:

- PostgreSQL
- Redis
- scanner
- worker
- Telegram
- broker mock
- order manager
- reconciliation

## Frontend

Test:

- login
- token refresh
- theme
- navigation
- scanner
- strategies
- risk
- orders
- positions
- permissions

Use Playwright for critical journeys.

---

# TRADING SAFETY TESTS

Must explicitly test:

### Duplicate order

Same signal submitted twice.

Expected:

one broker order only.

### Broker timeout

Order request times out after broker may have received it.

Expected:

UNKNOWN state.

Reconcile.

Never blind-resubmit.

### Redis outage

Expected:

safe degraded behavior.

### PostgreSQL outage

Expected:

stop execution.

### Market feed stale

Expected:

block new orders.

### Worker restart

Expected:

state recovery.

### Broker reconnect

Expected:

position/order reconciliation before execution resumes.

### Emergency stop

Expected:

no new orders.

### Daily loss reached

Expected:

trading halted.

### Race condition

Two simultaneous strategy signals compete for remaining risk.

Expected:

transactional risk reservation prevents over-allocation.

---

# CI/CD

Fix and enhance GitHub Actions.

Branches:

- main

Run on pull request and pushes where appropriate.

CI should include:

Backend:

- Ruff
- formatting
- type checks where practical
- pytest
- migrations check

Frontend:

- ESLint
- TypeScript
- build
- Playwright

Security:

- dependency vulnerability scan
- secret scan where practical

Do not deploy production automatically from an untested commit.

---

# BACKUPS

Implement documented backup strategy.

PostgreSQL:

- scheduled dump
- retention policy
- restore procedure

Configuration:

- secrets not inside backup repository
- protected encryption key

Document disaster recovery.

---

# UI DETAIL REQUIREMENTS

## Light Mode

Must be genuinely designed.

Not simply inverted dark mode.

Use:

- subtle neutral background
- crisp borders
- strong readable typography
- restrained green/red trading colors
- high contrast market numbers

## Dark Mode

Use a professional neutral/slate trading terminal.

Avoid pure black everywhere.

Use layered surfaces.

## Theme Persistence

Store user preference.

Options:

- Light
- Dark
- System

Prevent flash of incorrect theme during initial render.

---

# PROFESSIONAL INTERACTIONS

Use:

- skeleton loaders
- empty states
- retry actions
- connection indicators
- toast messages
- confirmation dialogs
- optimistic updates only where safe

Trading operations require confirmation depending on mode.

Emergency stop must be immediately accessible.

---

# NOTIFICATION SEVERITY

Create levels:

- INFO
- SUCCESS
- WARNING
- CRITICAL

Critical alerts:

- emergency stop
- broker disconnect during open position
- reconciliation mismatch
- live order rejected
- database failure
- stale market data during LIVE
- unknown order

---

# PHASED IMPLEMENTATION

Execute the following phases sequentially and automatically.

---

# PHASE 0 — BASELINE

- inspect repository
- run existing backend tests
- run frontend lint/build/tests
- document baseline failures
- fix environment-independent baseline failures
- verify Git branch = main

Continue automatically.

---

# PHASE 1 — FOUNDATION FIXES

Complete:

1. CI `main` alignment
2. exchange timestamp fix
3. strategy configuration wiring
4. access-token refresh
5. HTTP error handling
6. Telegram config consolidation
7. same-origin WebSockets
8. Docker exposure
9. non-root containers
10. trading calendar
11. data-quality validation
12. better worker exception handling

Run tests.

Continue automatically.

---

# PHASE 2 — PREMIUM UI / UX

Implement:

- design tokens
- Light/Dark/System
- premium layout
- sidebar
- header
- dashboard
- responsive system
- typography
- tables
- status indicators
- component refactor

Maintain all backend functionality.

Run frontend tests.

Continue automatically.

---

# PHASE 3 — PROFESSIONAL SCANNER

Build:

- real-time scanner workspace
- detailed setup inspector
- charts
- score explanation
- rejected setup reasons
- filtering
- sorting
- watchlists
- data-quality state

Run tests.

Continue automatically.

---

# PHASE 4 — STRATEGY PLATFORM

Create:

- StrategyRegistry
- strategy definitions
- versioning
- strategy configuration
- deterministic execution
- existing ORB migration
- strategy metrics

Run deterministic tests.

Continue automatically.

---

# PHASE 5 — ADVANCED PAPER TRADING

Implement:

- paper Order Manager
- paper fills
- positions
- fees
- slippage
- partial fill simulation
- orderbook
- P&L
- journal

No live broker orders.

Run extensive tests.

Continue automatically.

---

# PHASE 6 — RISK ENGINE

Implement:

- account limits
- trade risk
- daily/weekly limits
- position limits
- data-quality limits
- broker health
- market health
- RiskReservation
- emergency stop improvements

Run race-condition and risk tests.

Continue automatically.

---

# PHASE 7 — BACKTESTING

Implement:

- historical data
- backtest runs
- performance analytics
- costs
- slippage
- equity curves
- drawdown
- strategy comparison

Verify no look-ahead bias.

Continue automatically.

---

# PHASE 8 — OMS / EXECUTION CORE

Implement:

- OrderIntent
- idempotency
- Order Manager
- state machine
- Execution Gateway
- BrokerAdapter
- UpstoxAdapter
- FirstockAdapter interface
- reconciliation

KEEP LIVE EXECUTION DISABLED.

Run mocked broker tests.

Continue automatically.

---

# PHASE 9 — SHADOW MODE

Implement:

real market

+ real strategy

+ real risk

+ real intended order

but:

NO broker submission.

Compare shadow execution against paper assumptions.

Build analytics.

Continue automatically.

---

# PHASE 10 — ASSISTED TRADING

Implement:

Signal

→ Risk

→ Web/Telegram approval

→ Risk revalidation

→ broker execution

Only enable where external broker credentials and account requirements allow.

If broker credentials are unavailable:

complete the entire assisted architecture using mock/sandbox adapter and leave actual broker execution disabled.

Continue automatically.

---

# PHASE 11 — FULL AUTOMATION

Build the LIVE architecture.

Do NOT force-enable LIVE.

Provide configuration/gates.

Full flow:

Market Data

→ Strategy

→ Risk

→ OrderIntent

→ Execution

→ Broker

→ Fill

→ Position

→ Reconciliation

→ Journal

→ Analytics

The system must support full automation only when all activation gates pass.

---

# PHASE 12 — PRODUCTION HARDENING

Complete:

- security
- Docker
- reverse proxy
- TLS instructions
- monitoring
- backups
- recovery
- logging
- resource handling
- graceful shutdown
- startup reconciliation
- documentation

---

# PHASE 13 — FINAL QA

Perform complete final audit.

Run:

- backend tests
- integration tests
- frontend lint
- frontend build
- Playwright
- Docker build
- migrations
- security review

Fix failures.

---

# PHASE 14 — DOCUMENTATION

Update/create professional documentation:

- README.md
- ARCHITECTURE.md
- SECURITY.md
- TRADING_SAFETY.md
- DEPLOYMENT.md
- BROKER_INTEGRATION.md
- RISK_ENGINE.md
- OMS.md
- BACKTESTING.md
- TELEGRAM.md
- DISASTER_RECOVERY.md

Also include:

`docs/architecture/`

with Mermaid diagrams for:

- system architecture
- trading lifecycle
- order state machine
- broker integration
- deployment
- authentication

---

# CODING STANDARDS

Backend:

- strongly typed Python
- meaningful service boundaries
- dependency injection where useful
- async-safe patterns
- explicit transaction boundaries
- structured errors
- no silent broad exception swallowing

Frontend:

- strict TypeScript
- reusable components
- feature architecture
- hooks
- accessible controls
- consistent API contracts

Database:

- migrations
- constraints
- indexes
- transactions
- timestamps

---

# NO FAKE IMPLEMENTATIONS

Do not create buttons that do nothing.

Do not create fake charts presented as live data.

Do not create placeholder execution endpoints that return success when no execution occurred.

When functionality is unavailable, clearly label it:

- NOT CONFIGURED
- DISCONNECTED
- SIMULATION
- PAPER
- SHADOW
- DISABLED

---

# DO NOT HIDE ERRORS

Application errors must be:

- logged
- traceable
- displayed appropriately
- classified where useful

Never silently ignore critical execution failures.

---

# GIT WORKFLOW

Work from:

`main`

Before significant changes:

- inspect git status

Keep commits logical.

Examples:

`fix: align CI with main branch`

`fix: use exchange timestamp for candle aggregation`

`refactor: introduce strategy registry`

`feat: add premium trading dashboard`

`feat: add paper order manager`

`feat: implement risk reservation engine`

`feat: add broker-independent OMS`

`test: add duplicate order safety coverage`

Do not commit:

- `.env`
- tokens
- credentials
- browser auth artifacts
- Playwright traces containing secrets
- database dumps containing sensitive information

Update `.gitignore` accordingly.

---

# IMPORTANT DEVELOPMENT PHILOSOPHY

Always prefer:

correctness > convenience

safety > aggressive automation

determinism > cleverness

auditability > hidden behavior

reconciliation > assumptions

idempotency > blind retries

risk controls > strategy signals

---

# END GOAL

The final Sidra Algo Trading platform should feel like a serious professional trading system rather than a hobby scanner.

It should provide:

## Premium UI

- professional trading-terminal appearance
- Light/Dark/System themes
- responsive desktop
- fast interaction
- charts
- professional tables
- real-time status

## Trading

- scanner
- strategies
- signals
- paper
- backtest
- shadow
- assisted
- automatic execution

## Execution

- OMS
- idempotency
- broker abstraction
- fills
- positions
- reconciliation

## Risk

- professional Risk Engine
- risk reservations
- loss limits
- exposure controls
- stale-data protection
- emergency stop

## Operations

- system health
- audit trail
- Telegram
- user privileges
- secure broker credentials
- monitoring
- backup/recovery

## Engineering

- clean architecture
- maintainable code
- strong tests
- proper database design
- production security
- documentation

---

# FINAL INSTRUCTION

Start by auditing the current repository.

Do not ask me to approve each implementation phase.

Complete Phase 0.

Then automatically continue through Phase 1, Phase 2, Phase 3 and all subsequent phases.

At each phase:

- inspect
- implement
- test
- fix
- document
- continue

Do not pause merely because a phase is complete.

Only stop for a genuine external dependency that cannot be solved from the repository or local environment.

When such a dependency occurs, clearly document it, continue everything else possible, and leave the blocked feature safely disabled.

Under no circumstances should LIVE real-money execution be silently enabled.

The objective is to build the complete professional **Sidra Algo Trading V2** platform while preserving and improving the reliable functionality already present in the repository.