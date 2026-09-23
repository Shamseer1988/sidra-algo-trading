#!/bin/sh
# Read-only paper-trading summary. Run after the close for a daily snapshot:
#
#   scripts/paper-report.sh
#
# READ THE NET SECTION, NOT THE GROSS ONE.
#
# paper_signal_outcomes.realized_r is derived from the signal's own entry, target and
# stop prices. It assumes a fill at the signal price and an exit at exactly the target
# or stop, with no slippage and no fees. The realistic simulation — actual fill prices,
# slippage, brokerage, STT, exchange charge, GST, SEBI, stamp duty — lives in a separate
# ledger, paper_fills, and the two ledgers do not know about each other. A gross figure
# reported as the headline is how a strategy looks profitable until it trades. The NET
# section joins each resolved signal to its own fills and subtracts that trade's costs
# from that trade's R.
#
# R is used rather than rupees because R is invariant to the account_capital figure in
# trading_controls. With the fixed +minimum_rr / -1.0 payoff this build uses, win rate is
# the only variable that moves the gross result: breakeven is 1 / (1 + minimum_rr), so
# 40% at the default 1.5. Costs move it. If costs run c R per trade the payoff becomes
# (minimum_rr - c) against (1 + c), so at 1.5 RR breakeven becomes (1 + c) / 2.5 — 0.4 R
# of costs turns 40% into 56%.
#
# The SQL is written to a temporary file and piped in, rather than passed through nested
# shell quoting. An earlier version escaped single quotes inside a quoted docker argument
# and three sections silently failed at runtime, parsing their string literals as column
# names. A heredoc with a quoted delimiter expands nothing, so that class of bug cannot
# recur here.
#
# Sections that need tables from a newer schema are skipped rather than allowed to error,
# so this runs unchanged against an older deployment.
#
# Set SINCE to exclude earlier sessions, which matters after a settings change:
# results gathered under a different account size or risk budget describe a
# different system and must not be pooled with results gathered under this one.
#
#   SINCE=2026-09-17 scripts/paper-report.sh
#
# Nothing here writes; it is safe to run at any time, including mid-session.

set -e

PATH=/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin
export PATH

PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT"

[ -f .env ] || { echo "Missing .env in $PROJECT" >&2; exit 1; }

# Unset means every session ever recorded.
SINCE=${SINCE:-1900-01-01}

# psql reading its script from stdin, so no SQL ever passes through shell quoting.
run_sql() {
  docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -'
}

run_sql_value() {
  docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -f -'
}

# Greps for a distinctive token rather than comparing the whole output, so a
# login shell that prints a banner, or any other stray line, cannot be mistaken
# for the answer. A failure to reach the database reads as absent, which skips a
# section rather than erroring.
table_exists() {
  printf "select case when to_regclass('public.%s') is null then 'SIDRA_TABLE_ABSENT' else 'SIDRA_TABLE_PRESENT' end;\n" "$1" \
    | run_sql_value 2>/dev/null | grep -q SIDRA_TABLE_PRESENT
}

{
  printf "\\set since '%s'\n" "$SINCE"
  cat <<'SQL'
\echo
\echo Sessions from :'since' onward.
\echo === SESSIONS ===
select session_date, count(*) as signals
  from paper_signals
 where session_date >= :'since'::date
 group by 1 order by 1;

\echo === SIGNAL QUALITY, GROSS (every signal, whether or not the account could take it) ===
select s.strategy_version,
       count(*)                                 as trades,
       count(*) filter (where o.realized_r > 0) as wins,
       round(100.0 * count(*) filter (where o.realized_r > 0)
             / nullif(count(*), 0), 1)          as win_pct,
       round(avg(o.realized_r), 2)              as avg_r,
       round(sum(o.realized_r), 2)              as total_r,
       round(avg(o.mae_r), 2)                   as avg_mae_r,
       round(avg(o.mfe_r), 2)                   as avg_mfe_r
  from paper_signals s
  join paper_signal_outcomes o on o.paper_signal_id = s.id
 where o.status <> 'OPEN'
   and s.session_date >= :'since'::date
 group by 1 order by total_r desc;

\echo === EXECUTED TRADES ONLY, NET OF COSTS (signals the account actually took) ===
with resolved as (
  select s.id, s.strategy_version, s.risk_amount, o.realized_r
    from paper_signals s
    join paper_signal_outcomes o on o.paper_signal_id = s.id
   where o.status in ('TARGET', 'STOP')
     and o.realized_r is not null
     and s.risk_amount > 0
     and s.session_date >= :'since'::date
), costs as (
  select po.paper_signal_id,
         sum(f.total_fees + f.slippage_amount) as cost
    from paper_orders po
    join paper_fills f on f.paper_order_id = po.id
   group by 1
)
select r.strategy_version,
       count(*)                                                          as trades,
       round(sum(r.realized_r), 2)                                       as gross_r,
       round(sum(c.cost), 2)                                as costs_rs,
       round(avg(c.cost / r.risk_amount), 3)                as cost_per_trade_r,
       round(sum(r.realized_r - c.cost / r.risk_amount), 2) as net_r,
       round(avg(r.realized_r - c.cost / r.risk_amount), 3) as avg_net_r
  from resolved r
  join costs c on c.paper_signal_id = r.id
 group by 1 order by net_r desc;

\echo === SCORE VS OUTCOME (does the score filter predict anything?) ===
select width_bucket(s.score, 60, 101, 4)                         as band,
       min(s.score)                                              as from_score,
       max(s.score)                                              as to_score,
       count(*)                                                  as signals,
       count(*) filter (where o.realized_r > 0)                  as wins,
       round(100.0 * count(*) filter (where o.realized_r > 0)
             / nullif(count(*), 0), 1)                           as win_pct,
       round(avg(o.realized_r), 2)                               as avg_r,
       round(avg(o.mfe_r), 2)                                    as avg_mfe_r
  from paper_signals s
  join paper_signal_outcomes o on o.paper_signal_id = s.id
 where o.status in ('TARGET', 'STOP')
   and s.session_date >= :'since'::date
 group by 1 order by 1;

\echo === POSITION SIZE (paper sizing against the capital you intend to trade) ===
select round(avg(s.entry_price * s.quantity), 0) as avg_notional_rs,
       round(avg(s.risk_amount), 2)              as avg_risk_rs,
       round(avg(s.quantity), 1)                 as avg_qty,
       (select value::jsonb->>'account_capital'
          from application_settings where key = 'trading_controls')        as capital_rs,
       (select value::jsonb->>'risk_per_trade_percent'
          from application_settings where key = 'trading_controls')        as risk_pct,
       (select value::jsonb->>'slippage_bps'
          from application_settings where key = 'paper_execution_controls') as slippage_bps
  from paper_signals s
 where s.session_date >= :'since'::date;

\echo === ACCOUNT RESULT (the money, from the position ledger) ===
select p.session_date,
       count(*)                                                                as positions,
       count(*) filter (where p.realized_pnl - p.fees_total > 0)               as wins,
       round(sum(p.realized_pnl), 2)                                           as gross_pnl_rs,
       round(sum(p.fees_total), 2)                                             as fees_rs,
       round(sum(p.realized_pnl - p.fees_total), 2)                            as net_pnl_rs,
       round(sum((p.realized_pnl - p.fees_total) / nullif(s.risk_amount, 0)), 2) as net_r
  from paper_positions p
  join paper_signals s on s.id = p.paper_signal_id
 where p.session_date >= :'since'::date
 group by 1 order by 1;

\echo === SIGNALS THE ACCOUNT COULD NOT TAKE (its size declining its own strategy) ===
select r.session_date,
       count(*)                                                       as signals,
       count(*) filter (where r.decision_reason = 'Paper risk reserved'
                           or r.decision_reason like 'Paper position closed%') as accepted,
       count(*) filter (where r.decision_reason not like 'Paper risk reserved'
                         and r.decision_reason not like 'Paper position closed%') as declined,
       round(100.0 * count(*) filter (where r.decision_reason not like 'Paper risk reserved'
                                        and r.decision_reason not like 'Paper position closed%')
             / nullif(count(*), 0), 1)                                as declined_pct
  from risk_reservations r
 where r.session_date >= :'since'::date
 group by 1 order by 1;

\echo === RISK GATES, BY REASON ===
select decision_reason, count(*)
  from risk_reservations
 where session_date >= :'since'::date
 group by 1 order by 2 desc;

\echo === COSTS AND FILLS (every fill in range, not only resolved trades) ===
select count(*)                        as fills,
       round(sum(f.slippage_amount), 2) as slippage,
       round(sum(f.total_fees), 2)      as fees_total
  from paper_fills f
  join paper_orders o on o.id = f.paper_order_id
 where o.session_date >= :'since'::date;

\echo === STILL OPEN ===
select status, count(*) from paper_positions group by 1 order by 2 desc;
SQL

  if table_exists live_shadow_decisions; then
    cat <<'SQL'

\echo === LIVE SHADOW (what the live path would have decided; nothing was submitted) ===
select count(*)                                                     as evaluated,
       count(*) filter (where authorized)                           as would_authorize,
       count(*) filter (where translation_status <> 'RESOLVED')     as unmapped_symbols,
       round(100.0 * count(*) filter (where authorized)
             / nullif(count(*), 0), 1)                              as authorize_pct
  from live_shadow_decisions;

\echo === LIVE SHADOW REFUSALS (which gate said no, most frequent first) ===
select check_key, count(*)
  from live_shadow_decisions,
       jsonb_array_elements_text(failed_checks::jsonb) as check_key
 where not authorized
 group by 1 order by 2 desc;
SQL
  else
    cat <<'SQL'

\echo === LIVE SHADOW: not deployed on this build, section skipped ===
SQL
  fi
} | run_sql
