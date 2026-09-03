# Testing & Quality Verification

Intraday Sentinel implements an automated testing and release verification matrix covering unit, integration, load, chaos/recovery, and browser E2E suites.

---

## 1. Test Categories & Commands

### Backend Tests (Unit, Load, Recovery)
```bash
# Run all backend tests
pytest apps/api/tests -v

# Run unit tests only
pytest apps/api/tests/test_*.py -v

# Run load & concurrency benchmarks (50 concurrent users, 100 WebSockets, candle throughput)
pytest apps/api/tests/load -v

# Run chaos & recovery tests (Redis/PostgreSQL disconnects, worker restarts, deduplication)
pytest apps/api/tests/recovery -v
```

### Code Quality & Formatting
```bash
# Check Python code linting
python -m ruff check apps/api

# Check Python formatting
python -m ruff format --check apps/api

# Auto-format Python code
python -m ruff format apps/api
```

### Frontend Build & Browser E2E Tests (Playwright)
```bash
# Lint frontend code
npm run lint:web

# Build production Next.js bundle
npm run build:web

# Run headless Playwright browser tests
npm run test:e2e
```

---

## 2. Release Acceptance
Detailed SLAs, performance thresholds, failure recovery logs, and the complete release checklist are documented in [RELEASE_GATES.md](RELEASE_GATES.md).
