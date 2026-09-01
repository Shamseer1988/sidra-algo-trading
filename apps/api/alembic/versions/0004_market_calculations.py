"""add completed market candles and calculation snapshots

Revision ID: 0004_market_calculations
Revises: 0003_telegram_control_plane
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_market_calculations"
down_revision = "0003_telegram_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_candles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("timeframe_seconds", sa.Integer(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tick_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("instrument_token", "timeframe_seconds", "opened_at", name="uq_market_candle_bucket"),
    )
    op.create_index("ix_market_candles_instrument_token", "market_candles", ["instrument_token"])
    op.create_index("ix_market_candles_session_date", "market_candles", ["session_date"])
    op.create_index("ix_market_candles_created_at", "market_candles", ["created_at"])
    op.create_index("ix_market_candles_instrument_opened", "market_candles", ["instrument_token", "opened_at"])

    op.create_table(
        "market_indicator_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("timeframe_seconds", sa.Integer(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("candle_opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "instrument_token", "timeframe_seconds", "candle_opened_at", name="uq_market_indicator_snapshot"
        ),
    )
    op.create_index(
        "ix_market_indicator_snapshots_instrument_token", "market_indicator_snapshots", ["instrument_token"]
    )
    op.create_index("ix_market_indicator_snapshots_session_date", "market_indicator_snapshots", ["session_date"])
    op.create_index("ix_market_indicator_snapshots_created_at", "market_indicator_snapshots", ["created_at"])
    op.create_index(
        "ix_market_indicator_snapshots_instrument_created",
        "market_indicator_snapshots",
        ["instrument_token", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("market_indicator_snapshots")
    op.drop_table("market_candles")
