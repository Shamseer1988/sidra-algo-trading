# TASK: Complete Phase 9 Release Gates & Hardening for Intraday Sentinel

Repository Context:
- `apps/api`: FastAPI backend with SQLAlchemy (asyncio), PostgreSQL, Redis, Alembic, and WebSocket event streaming.
- `apps/web`: Next.js 15 (App Router), React 19, Tailwind CSS, TypeScript frontend.
- `docs/`: System documentation and specifications.

---

## 1. Browser Tests (Playwright E2E)
- In `apps/web`: Install `@playwright/test` and browsers.
- Create Playwright config (`apps/web/playwright.config.ts`).
- Add tests in `apps/web/tests/e2e/terminal.spec.ts` covering:
  - Login & logout authentication flow with error handling.
  - Role-Based Access Control (RBAC) restrictions for VIEWER / TRADER / ADMIN.
  - Scanner controls (start & stop triggers and live badge state).
  - Signals Explorer filters (query search, side filter LONG/SHORT/ALL) & SVG completed-candle chart view.
  - Risk & Trading settings modification, validation, and audit log tracking.
  - Session revocation in the Security panel.
  - CSV Export: Add "Export CSV" button in `apps/web/components/app-shell.tsx` linked to `/api/v1/journal/export.csv` and test download triggering.

---

## 2. Load & Concurrency Tests
- Create automated load tests in `apps/api/tests/load/test_system_load.py` using `pytest` + `asyncio` / `httpx` / `websockets`:
  - 50 Concurrent Authenticated Users: Test `/auth/me`, `/system/overview`, `/scanner/signals`, `/settings/trading`, `/journal/summary` with target p95 < 200ms and 0% errors.
  - 100 Concurrent WebSocket Clients: Test `/api/v1/events/scanner` message broadcasts, heartbeat ping/pong, and zero connection drops.
  - Sustained Candle Ingestion: Ingest high-volume ticks and 1-minute completed candles to verify memory stability and non-blocking asyncio event loop.
  - Resource Utilization: Track memory footprint and connection pooling limits.

---

## 3. Failure & Recovery Tests
- Create automated fault recovery tests in `apps/api/tests/recovery/test_resilience_and_recovery.py`:
  - Redis Disconnection & Reconnection during active scanning.
  - PostgreSQL Disconnection, clean rollbacks, and pool reconnection.
  - Scanner Worker & API crash/restart: safe pause state, candle & signal deduplication (idempotency), and alert suppression for existing signals.
  - Invariant Verification: Formally assert `LIVE_TRADING_ENABLED = False` across all recovery workflows.

---

## 4. Quality Cleanup & CI Pipeline
- Add `tzdata` to `apps/api/pyproject.toml` dependencies (for Windows `Asia/Kolkata` ZoneInfo support).
- Configure `[tool.ruff]` and `[tool.ruff.lint]` in `apps/api/pyproject.toml`.
- Fix all Ruff findings across `apps/api/` (`ruff check apps/api` and `ruff format --check apps/api` must pass with 0 errors).
- Create `.github/workflows/ci.yml` running lint, backend unit/integration tests, web build, Playwright browser tests, and load smoke checks.

---

## 5. Documentation & Release Acceptance
- Create `docs/RELEASE_GATES.md` with:
  - CLI test commands.
  - Performance SLA targets and latency thresholds.
  - Full Phase 9 Release Acceptance Checklist.
- Update `docs/TESTING.md` and `IMPLEMENTATION_PLAN.md`.
