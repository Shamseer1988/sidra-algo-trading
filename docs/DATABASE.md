# Database

PostgreSQL is required in all non-test environments. Initial Phase 1 models are users, user sessions, login history, and audit logs. Alembic owns schema changes; `AUTO_CREATE_SCHEMA=true` exists only for an empty local-development bootstrap and must be disabled for deployed environments.

Planned time-series tables (`candles`, `signals`, `market_regimes`, and paper-trade events) will persist UTC timestamps and receive composite indexes beginning with instrument/symbol plus timestamp. Partitioning will be introduced after access patterns and retention requirements are measured.
