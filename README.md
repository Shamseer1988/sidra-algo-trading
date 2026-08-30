# Intraday Sentinel

Self-hosted, paper-first NSE intraday scanning and outbound Telegram alerting platform. Release 1 has no order submission path. `LIVE_TRADING_ENABLED=false` is the mandatory default.

## Current state

Phase 1 foundation is scaffolded: Docker topology, FastAPI API, database models/migrations, authentication primitives, scanner worker lifecycle, Next.js terminal shell, environment template, and Windows scripts. Firstock connectivity, market data, strategies, and Telegram delivery are intentionally planned for later phases.

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

3. Open `http://localhost` and API docs at `http://localhost:8000/docs`.
4. Create the first administrator after the stack is healthy:

   ```powershell
   docker compose exec api python -m app.cli create-admin --email admin@example.com
   ```

The command prompts for a password; one is never shipped in the repository.

For a production-style deployment, run migrations before starting the API with `docker compose exec api alembic upgrade head` and set `AUTO_CREATE_SCHEMA=false`.

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
