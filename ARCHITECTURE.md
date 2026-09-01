# Sidra Algo Trading Architecture

## Purpose and safety boundary

Sidra Algo Trading is a self-hosted NSE trading platform currently operating as a paper-only scanner. It ingests market data, creates completed candles, evaluates deterministic strategies, sends Telegram alerts, and journals paper outcomes. It does **not** submit orders. `LIVE_TRADING_ENABLED` defaults to `false`; future execution interfaces require explicit safety and compliance gates.

## Runtime topology

```text
Browser ──HTTP──> Next.js web ──same-origin proxy──> FastAPI API
   │                                                   ▲
   └──authenticated scanner WebSocket──────────────────┘
                                                              │
                                                    Redis <───┼──> scanner worker
                                                              │
                                                         PostgreSQL
                                                              │
                                      Firstock REST/WebSocket (future adapter)
                                      Telegram sendMessage only (future service)
```

Docker exposes only the web application on `127.0.0.1:${WEB_PORT:-3001}`. FastAPI, PostgreSQL, Redis, and the scanner worker live on internal Docker networks and are not internet-facing.

## Modules

| Area | Responsibility |
| --- | --- |
| `apps/api/app/api` | Versioned HTTP and authenticated WebSocket entry points; no trading rules. |
| `apps/api/app/domain` | Entities, enums, and strategy-independent business types. |
| `apps/api/app/services` | Authentication, health, scanner orchestration, notifications, and future broker adapters. |
| `apps/api/app/db` | SQLAlchemy models, sessions, repositories, and Alembic migrations. |
| `services/*` | Future independently testable market-data, scanner, strategy, and notification packages. |
| `apps/web` | Next.js terminal UI and same-origin backend proxy. |

## Data and time

PostgreSQL is the source of truth for users, complete candles, signals, alert history, paper trades, audit records, and configuration. Redis holds short-lived ticks, incomplete candles, locks, cooldowns, and fan-out events. Times are persisted in UTC and market-session calculations use `Asia/Kolkata` explicitly.

## Security model

Broker and Telegram secrets stay server-side. Browser authentication uses HttpOnly cookies; no auth token is stored in local storage. Passwords use Argon2id. Role checks are enforced by the API. Configuration values are masked in future settings responses. Structured logs must never include secrets.

## Future execution boundary

`OrderExecutionService`, `OrderManager`, and `PositionManager` are reserved interfaces. No router, worker, or Firstock adapter may submit orders in Release 1. Any future implementation must require an explicit server-side enablement, health/risk checks, and an audit event.
