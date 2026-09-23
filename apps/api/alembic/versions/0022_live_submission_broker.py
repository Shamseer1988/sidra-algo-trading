"""Record which broker a live submission and approval belong to.

An operator can now choose between Upstox and Firstock, which makes "where did
this order go" a real question for the first time. A submission row that does
not answer it cannot be reconciled against anything: the order book it belongs
to is the first thing somebody resolving an UNKNOWN has to know.

Existing rows get an empty string rather than a guess. No live order has been
placed by this system, so there is nothing to backfill, and writing FIRSTOCK
into rows nobody sent would be inventing history that reconciliation would then
believe.

Revision ID: 0022_live_submission_broker
Revises: 0021_live_submission
"""

import sqlalchemy as sa
from alembic import op

revision = "0022_live_submission_broker"
down_revision = "0021_live_submission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "live_order_submissions",
        sa.Column("broker", sa.String(length=20), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_live_order_submissions_broker",
        "live_order_submissions",
        ["broker"],
    )
    # On the approval too: an operator approves an order at a particular broker,
    # and the selection can change between the question and the answer.
    op.add_column(
        "live_order_approvals",
        sa.Column("broker", sa.String(length=20), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("live_order_approvals", "broker")
    op.drop_index("ix_live_order_submissions_broker", table_name="live_order_submissions")
    op.drop_column("live_order_submissions", "broker")
