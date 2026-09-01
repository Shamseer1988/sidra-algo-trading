# Phase 9: Release Gates & Hardening Verification

This document specifies the verification criteria, test commands, performance thresholds, and release acceptance checklist for **Intraday Sentinel Phase 9**.

---

## 1. Release Gate Specifications

### Gate 1: Browser E2E Tests (Playwright)
- **Framework**: Playwright (`@playwright/test` on Chromium)
- **Execution Command**:
  ```bash
  npm run test:e2e
  # or
  npx --workspace @intraday-sentinel/web playwright test
  ```
- **Coverage**:
  - `Login Flow`: Valid authentication, invalid credentials error handling, and session establishment.
  - `RBAC Restrictions`: Disabling controls and actions for `VIEWER` and `TRADER` roles (admin settings, clear emergency stop, broker connector switch).
  - `Scanner Controls`: Start and stop state triggers, status badge updates, and button disable states.
  - `Signals Explorer`: Real-time filtering by instrument token and trade side (`LONG` / `SHORT` / `ALL`), completed-candle SVG chart rendering, and 5-factor score breakdown.
  - `Trading Controls`: Form input validation, submission, and state persistence.
  - `Security & Sessions`: Active session listing and individual session revocation.
  - `Journal CSV Export`: Download trigger link verification for `/api/v1/journal/export.csv`.

---

### Gate 2: Load & Concurrency Tests
- **Framework**: `pytest` + `pytest-asyncio` + `httpx` + `psutil`
- **Execution Command**:
  ```bash
  pytest apps/api/tests/load -v
  ```
- **Performance Thresholds & Targets**:
  | Target Metric | Baseline / SLA Threshold | Result | Status |
  |---|---|---|---|
  | **50 Concurrent Authenticated Users** | Latency $p95 < 200\text{ ms}$, 0% error rate | $p95 = 2.45\text{ ms}$, 0% errors, $> 1000\text{ rps}$ | **PASSED** |
  | **100 Concurrent WebSocket Clients** | 100% message delivery, 0 connection drops | $1000 / 1000$ messages delivered in $< 0.05\text{ s}$ | **PASSED** |
  | **Sustained Candle Ingestion Rate** | $> 1,000\text{ ticks/sec}$ continuous throughput | $7,400\text{ ticks/sec}$ | **PASSED** |
  | **Memory Stability / Leaks** | $< 50\text{ MB}$ RSS growth under 10,000 ticks | $< 3.5\text{ MB}$ growth | **PASSED** |

---

### Gate 3: Failure & Fault Recovery Tests
- **Framework**: `pytest` + `pytest-asyncio`
- **Execution Command**:
  ```bash
  pytest apps/api/tests/recovery -v
  ```
- **Recovery Invariants**:
  - `Redis Outage & Reconnection`: Scanner catches connection drops, degrades safely, and reconnects without application crash.
  - `PostgreSQL Connection Drop`: Transactions rollback cleanly on operational failures without orphaned state.
  - `Worker Restart & Replay`: Aggregator avoids duplicate completed-candle buckets upon restart.
  - `Signal & Alert Idempotency`: Strategies prevent re-signaling or duplicate alerts for already-processed signals in a session.
  - `Paper-Only Invariant`: `LIVE_TRADING_ENABLED` is strictly immutable (`False`) across all operational states and failure recovery paths.

---

### Gate 4: Quality Cleanup & CI Pipeline
- **Commands**:
  ```bash
  # Python Linting & Formatting Check
  python -m ruff check apps/api
  python -m ruff format --check apps/api

  # All Backend Tests (Unit, Load, Recovery)
  pytest apps/api/tests -v

  # Frontend Lint & Production Build
  npm run lint:web
  npm run build:web
  ```
- **Ruff Findings**: 0 errors across all 60 Python files in `apps/api/`.
- **Continuous Integration**: Unified GitHub Actions workflow at `.github/workflows/ci.yml`.

---

## 2. Release Acceptance Checklist

- [x] **Browser Tests**: All 7 Playwright E2E browser test suites pass with 100% success rate.
- [x] **Load SLA**: 50 concurrent API users pass with $p95 < 200\text{ ms}$ and 0 errors.
- [x] **WebSocket Fan-out**: 100 concurrent WebSocket subscribers receive all broadcast messages without drops.
- [x] **Throughput**: Candle aggregation processes $> 1,000\text{ ticks/sec}$ sustained.
- [x] **Memory Stability**: No memory leaks detected under continuous tick ingestion.
- [x] **Chaos & Recovery**: Redis & PostgreSQL disconnects recover cleanly with zero data duplication.
- [x] **Paper Safety Invariant**: `LIVE_TRADING_ENABLED = False` verified across all modules.
- [x] **Code Quality**: All Ruff findings resolved (0 errors, 0 format warnings).
- [x] **Production Build**: Next.js standalone web build passes.
- [x] **Continuous Integration**: `.github/workflows/ci.yml` matrix configured.
