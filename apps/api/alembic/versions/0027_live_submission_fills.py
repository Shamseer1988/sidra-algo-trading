"""Record what the broker actually filled, beside what we asked for.

An operator compared the P&L calendar against the Upstox app and found a ₹5.34
disagreement on a ₹170 day -- and on one of the two trades, a ₹0.58 per share
loss recorded against a ₹2.00 per share loss at the broker. Neither figure was
wrong about what it measured. The rows labelled LIVE were carrying the
simulator's fill prices, because ``average_entry_price`` and
``average_exit_price`` are written in exactly one place -- the paper execution
engine, filling at completed-candle prices -- and nothing had ever written a
broker fill back.

These four columns are where the broker's answer lands. They are deliberately
*not* a correction of the paper position: that record is what this system
believed at the time, it is what strategy evaluation is built on, and
overwriting it would destroy the only evidence of the difference. The
difference is slippage, and it is worth seeing.

``broker_status`` is kept apart from ``status`` for a sharper reason. The exit
manager finds a resting stop with ``status == ACCEPTED``; a stop whose status
had been advanced to the broker's word would not be found, would not be
cancelled, and the exit sent past it would reverse the position rather than
close it. The broker's word for an order goes in its own column.

Nullable throughout. A broker that did not report a fill is not a broker
reporting zero, and a zero here would read as "filled nothing" for every order
placed before this migration.

Revision ID: 0027_live_submission_fills
Revises: 0026_broker_day_snapshots
"""

import sqlalchemy as sa

from alembic import op

revision = "0027_live_submission_fills"
down_revision = "0026_broker_day_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("live_order_submissions", sa.Column("filled_quantity", sa.Integer(), nullable=True))
    op.add_column("live_order_submissions", sa.Column("average_fill_price", sa.Numeric(18, 4), nullable=True))
    op.add_column("live_order_submissions", sa.Column("broker_status", sa.String(length=30), nullable=True))
    op.add_column("live_order_submissions", sa.Column("fill_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("live_order_submissions", "fill_seen_at")
    op.drop_column("live_order_submissions", "broker_status")
    op.drop_column("live_order_submissions", "average_fill_price")
    op.drop_column("live_order_submissions", "filled_quantity")
