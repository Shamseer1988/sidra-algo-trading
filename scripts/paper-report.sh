#!/bin/sh
# Read-only paper-trading summary. Run after the close for a daily snapshot:
#
#   scripts/paper-report.sh
#
# Reports per-strategy performance in R-multiples rather than rupees, because R is
# invariant to the account_capital figure in trading_controls. With the fixed
# +minimum_rr / -1.0 payoff this build uses, win rate is the only variable that moves
# the result: breakeven is 1 / (1 + minimum_rr), so 40% at the default 1.5.
#
# Nothing here writes; it is safe to run at any time, including mid-session.

set -e

PATH=/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin
export PATH

PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT"

[ -f .env ] || { echo "Missing .env in $PROJECT" >&2; exit 1; }

docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
-c "\echo
\echo === SESSIONS ===" \
-c "select session_date, count(*) as signals from paper_signals group by 1 order by 1;" \
-c "\echo === PER-STRATEGY (closed trades, R-multiples) ===" \
-c "select s.strategy_version,
        count(*)                                 as trades,
        count(*) filter (where o.realized_r > 0) as wins,
        round(100.0 * count(*) filter (where o.realized_r > 0)
              / nullif(count(*),0), 1)           as win_pct,
        round(avg(o.realized_r), 2)              as avg_r,
        round(sum(o.realized_r), 2)              as total_r,
        round(avg(o.mae_r), 2)                   as avg_mae_r,
        round(avg(o.mfe_r), 2)                   as avg_mfe_r
   from paper_signals s
   join paper_signal_outcomes o on o.paper_signal_id = s.id
  where o.status <> '\''OPEN'\''
  group by 1 order by total_r desc;" \
-c "\echo === RISK GATES (anything other than accepted means signals were suppressed) ===" \
-c "select decision_reason, count(*) from risk_reservations group by 1 order by 2 desc;" \
-c "\echo === COSTS AND FILLS ===" \
-c "select count(*) as fills,
        round(sum(slippage_amount), 2) as slippage,
        round(sum(brokerage), 2)       as brokerage
   from paper_fills;" \
-c "\echo === STILL OPEN ===" \
-c "select status, count(*) from paper_positions group by 1 order by 2 desc;"'
