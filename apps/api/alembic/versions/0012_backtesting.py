"""add durable completed-candle backtesting ledger

Revision ID: 0012_backtesting
Revises: 0011_risk_reservations
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012_backtesting"
down_revision = "0011_risk_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="COMPLETED"),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("timeframe_seconds", sa.Integer(), nullable=False),
        sa.Column("instrument_tokens", sa.JSON(), nullable=False),
        sa.Column("strategy_snapshot", sa.JSON(), nullable=False),
        sa.Column("controls_snapshot", sa.JSON(), nullable=False),
        sa.Column("execution_snapshot", sa.JSON(), nullable=False),
        sa.Column("data_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_candle_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("initial_capital", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("final_equity", sa.Numeric(precision=18, scale=4)),
        sa.Column("net_pnl", sa.Numeric(precision=18, scale=4)),
        sa.Column("max_drawdown", sa.Numeric(precision=18, scale=4)),
        sa.Column("result_summary", sa.JSON(), nullable=False),
        sa.Column("failure_detail", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_backtest_runs_created_by_user_id", "backtest_runs", ["created_by_user_id"])
    op.create_index("ix_backtest_runs_status", "backtest_runs", ["status"])
    op.create_index("ix_backtest_runs_start_date", "backtest_runs", ["start_date"])
    op.create_index("ix_backtest_runs_end_date", "backtest_runs", ["end_date"])
    op.create_index("ix_backtest_runs_data_fingerprint", "backtest_runs", ["data_fingerprint"])
    op.create_index("ix_backtest_runs_created_status", "backtest_runs", ["created_at", "status"])
    op.create_index("ix_backtest_runs_dates", "backtest_runs", ["start_date", "end_date"])
    op.create_table(
        "backtest_trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trade_key", sa.String(length=220), nullable=False),
        sa.Column("strategy_id", sa.String(length=100), nullable=False),
        sa.Column("strategy_name", sa.String(length=120), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("signal_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("exit_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("gross_pnl", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("fees_total", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("net_pnl", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("realized_r", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("exit_reason", sa.String(length=40), nullable=False),
        sa.UniqueConstraint("run_id", "trade_key", name="uq_backtest_trades_run_key"),
    )
    op.create_index("ix_backtest_trades_run_id", "backtest_trades", ["run_id"])
    op.create_index("ix_backtest_trades_strategy_id", "backtest_trades", ["strategy_id"])
    op.create_index("ix_backtest_trades_instrument_token", "backtest_trades", ["instrument_token"])
    op.create_index("ix_backtest_trades_session_date", "backtest_trades", ["session_date"])
    op.create_index("ix_backtest_trades_run_exited", "backtest_trades", ["run_id", "exited_at"])
    op.create_index("ix_backtest_trades_strategy", "backtest_trades", ["strategy_id", "strategy_version"])


def downgrade() -> None:
    op.drop_table("backtest_trades")
    op.drop_table("backtest_runs")
