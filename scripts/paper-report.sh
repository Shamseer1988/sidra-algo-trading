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
# Read the NET section, not the GROSS one. paper_signal_outcomes.realized_r is derived
# from the signal's own entry/target/stop prices, so it assumes a fill at the signal
# price and an exit at exactly the target or stop, with no slippage and no fees. The
# realistic simulation lives in a separate ledger, paper_fills, and the two were never
# related to each other. A gross figure next to an unrelated costs total is how a
# strategy looks profitable until it trades. The NET section subtracts each trade's own
# fills from its own R, which is the number the go-live decision should rest on.
#
# Breakeven moves with costs. At 1.5 RR gross, breakeven is 40%; if costs run c R per
# trade, the payoff becomes (1.5 - c) against (1.0 + c) and breakeven becomes
# (1 + c) / (2.5), so 0.4 R of costs pushes it to 56%.
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
-c "\echo === PER-STRATEGY, GROSS (no costs: exits are assumed at the exact target or stop) ===" \
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
-c "\echo === PER-STRATEGY, NET OF COSTS (this is the number that decides go-live) ===" \
-c "with resolved as (
     select s.id, s.strategy_version, s.risk_amount, o.realized_r
       from paper_signals s
       join paper_signal_outcomes o on o.paper_signal_id = s.id
      where o.status in ('\''TARGET'\'','\''STOP'\'') and o.realized_r is not null and s.risk_amount > 0
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
          round(sum(coalesce(c.cost, 0)), 2)                                as costs_rs,
          round(sum(coalesce(c.cost, 0) / r.risk_amount), 2)                as costs_r,
          round(sum(r.realized_r - coalesce(c.cost, 0) / r.risk_amount), 2) as net_r,
          round(avg(r.realized_r - coalesce(c.cost, 0) / r.risk_amount), 3) as avg_net_r
     from resolved r
     left join costs c on c.paper_signal_id = r.id
    group by 1 order by net_r desc;" \
-c "\echo === POSITION SIZE (paper sizing vs the capital you intend to trade) ===" \
-c "select round(avg(s.entry_price * s.quantity), 0) as avg_notional_rs,
        round(avg(s.risk_amount), 2)              as avg_risk_rs,
        round(avg(s.quantity), 1)                 as avg_qty,
        (select value::jsonb->>'\''account_capital'\'' from application_settings where key = '\''trading_controls'\'')       as capital_rs,
        (select value::jsonb->>'\''risk_per_trade_percent'\'' from application_settings where key = '\''trading_controls'\'') as risk_pct,
        (select value::jsonb->>'\''slippage_bps'\'' from application_settings where key = '\''paper_execution_controls'\'')   as slippage_bps
   from paper_signals s;" \
-c "\echo === COSTS AND FILLS (all fills ever recorded, not just resolved trades) ===" \
-c "select count(*) as fills,
        round(sum(slippage_amount), 2) as slippage,
        round(sum(total_fees), 2)      as fees_total
   from paper_fills;" \
-c "\echo === STILL OPEN ===" \
-c "select status, count(*) from paper_positions group by 1 order by 2 desc;" \
-c "\echo === LIVE SHADOW (what the live path would have decided; nothing was submitted) ===" \
-c "select count(*)                                    as evaluated,
        count(*) filter (where authorized)             as would_authorize,
        count(*) filter (where translation_status <> '\''RESOLVED'\'') as unmapped_symbols,
        round(100.0 * count(*) filter (where authorized)
              / nullif(count(*),0), 1)                 as authorize_pct
   from live_shadow_decisions;" \
-c "\echo === LIVE SHADOW REFUSALS (which gate said no, most frequent first) ===" \
-c "select check_key, count(*)
   from live_shadow_decisions, jsonb_array_elements_text(failed_checks::jsonb) as check_key
  where not authorized
  group by 1 order by 2 desc;"'
