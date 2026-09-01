# Sidra Algo Trading

Self-hosted, paper-first NSE algorithmic trading command center. The current release has no order-submission path. `LIVE_TRADING_ENABLED=false` is the mandatory default.

## Current state

The available foundation includes Docker topology, FastAPI, database migrations,
protected login/RBAC, scanner safety controls, Upstox paper-market-data ingestion,
retained Firstock market-data support, completed one-minute candle calculations,
paper strategy signals, journal analytics, deterministic replay foundations, and
dedicated-bot Telegram control-plane support. A fail-closed NSE calendar and
per-instrument data-quality gates prevent unsafe signal evaluation. Every
order-submission path remains outside this release.

## Prerequisites

- Windows 10/11 with WSL2 and Docker Desktop
- Docker Desktop running
- Python 3.12+ for local API development
- Node.js 20.11+ (Node 24 is supported)

## Docker quick start

1. Copy `.env.example` to `.env` and replace `POSTGRES_PASSWORD` and `JWT_SECRET` with strong random values.
2. Start the stack:

   ```powershell
   .\scripts\start.ps1
   ```

3. Open the protected web terminal on the configured local web port (default `http://127.0.0.1:3001`). FastAPI stays private inside Docker and is reached through same-origin `/api` routes.
4. Create the first administrator after the stack is healthy:

   ```powershell
   docker compose exec api python -m app.cli create-admin --email admin@example.com
   ```

The command prompts for a password; one is never shipped in the repository.

For a production-style deployment, run migrations before starting the API with `docker compose exec api alembic upgrade head` and set `AUTO_CREATE_SCHEMA=false`.

## Sidra terminal

Sign in at the web address shown by `docker compose ps` (normally `http://127.0.0.1:3001`). The terminal reports actual API, PostgreSQL, Redis, market-feed, notification, and scanner-worker state. ADMIN users can change paper-risk controls and start or stop the scanner worker.

## Market-data connectors

Upstox is the default paper-market-data connector. Follow [docs/UPSTOX.md](docs/UPSTOX.md)
to set its server-only token and confirmed instrument keys, rebuild the stack, then
select **Use Upstox PAPER** in the protected **Settings** page. The connector uses
the market-data feed only; it has no order-submission implementation.

Firstock remains available as a disabled-by-default alternative for future
evaluation. Follow [docs/FIRSTOCK.md](docs/FIRSTOCK.md) if you choose to configure
that feed. Only one connector can be active at a time.

## Firstock market-data setup

The backend includes a Firstock V2 market-feed adapter. It remains disabled by
default; configure it only if you select it in Settings. It receives market data
only and has no order-placing API. Phase 4 persists completed one-minute candles and
calculates intraday indicators; see [docs/STRATEGY.md](docs/STRATEGY.md).

## Safety and Telegram controls

The terminal Control Plane provides scanner start/stop, paper-tracking enable/disable, emergency stop, and Telegram status. Live execution is intentionally locked in Release 1. Configure a dedicated Telegram bot according to [docs/TELEGRAM.md](docs/TELEGRAM.md); inbound Approve/Reject actions are stored as non-executing intents, while Emergency Stop halts the scanner.

## Signals terminal

The protected **Signals** workspace lists persisted paper-only scanner decisions with
filters, risk/target detail, and the strategy score breakdown. It receives a small
authenticated scanner-event notification when a new paper signal is recorded, with a
15-second API refresh fallback. The selected signal shows its authenticated,
persisted completed-candle chart with entry and stop overlays. It does not display
generated sample prices or expose an order action.

## Local development

Copy the environment file, set `POSTGRES_HOST=localhost`, and run PostgreSQL/Redis via Docker (ports should be exposed only for local development if needed). Then:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .\apps\api[dev]
uvicorn app.main:app --app-dir .\apps\api --reload
npm install
npm run dev:web
```

## Safety

- Do not put secrets in `NEXT_PUBLIC_*` environment variables.
- Never expose PostgreSQL or Redis to the public internet.
- Firstock and Telegram credentials have not been implemented or requested from the browser.
- See [ARCHITECTURE.md](ARCHITECTURE.md), [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md), and the `docs/` directory.
