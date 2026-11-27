-- A database role for the research worker that can read market data and write
-- research results, and nothing else. Research Lab Phase 4.
--
-- NOT APPLIED AUTOMATICALLY. Run once, by hand, with Shamseer's approval, as the
-- Postgres owner, after migration 0032 has created the research schema:
--
--   docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v research_password="'<choose one; never paste it into a chat>'" -f /dev/stdin < infra/sql/research_role.sql
--
-- Then give the research-worker its own DATABASE_URL using this role (an
-- `environment:` override in docker-compose.yml reading a new .env variable).
-- Until that is done the worker uses the application's role, and the locks in
-- app/research_worker.py are what keep it away from trading tables.
--
-- What it may do:
--   * read operational market data and the strategy settings it measures;
--   * read and write the research schema, the job queue, and backtest results;
--   * add audit rows.
-- What it may not do: touch any live_*, paper_*, oms_*, order or session table,
-- users, broker credentials, or change a setting.

CREATE ROLE sidra_research LOGIN PASSWORD :research_password;

GRANT CONNECT ON DATABASE :"DBNAME" TO sidra_research;  -- psql's own variable: the database connected to
GRANT USAGE ON SCHEMA public TO sidra_research;
GRANT USAGE, CREATE ON SCHEMA research TO sidra_research;

-- Read: market data, and the settings a run snapshots.
GRANT SELECT ON public.market_candles, public.market_indicator_snapshots, public.scan_universe,
    public.corporate_events, public.application_settings, public.instrument_master_refreshes
    TO sidra_research;

-- Read and write: research results and the job queue.
GRANT SELECT, INSERT, UPDATE ON public.backtest_runs, public.backtest_trades, public.backtest_sweeps,
    public.research_jobs, public.research_job_events
    TO sidra_research;
GRANT INSERT ON public.audit_logs TO sidra_research;

-- The research schema, now and later.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA research TO sidra_research;
ALTER DEFAULT PRIVILEGES IN SCHEMA research GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sidra_research;

-- Nothing else. In particular no grant on live_order_submissions, live_*,
-- paper_*, oms_*, order_intents, session_halts, users, user_sessions,
-- broker_credentials or setting_revisions.
