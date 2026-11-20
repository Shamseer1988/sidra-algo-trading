"""Record which desk -- intraday or swing -- each signal and live order belongs to.

Groundwork for a swing desk that holds delivery positions overnight beside the
intraday desk that is flat by the square-off. Nothing in the order path can tell
the two apart today: the product cannot (intraday without leverage is already
DELIVERY), and the session date cannot (a swing entry filled this morning sits
in today's position book next to the intraday trades).

So the desk is a column, written with the row, on the two tables the live path
reasons from: ``paper_signals``, where a strategy's decision is born, and
``live_order_submissions``, where every real order is recorded. Approvals and
shadow decisions point at their signal and take the desk from it.

**Safe to apply under a running deployment.** The column is NOT NULL with a
constant server default, which PostgreSQL 11+ adds as a catalogue change
without rewriting the table, and which fills every existing row with INTRADAY --
the truth, since every trade so far was intraday. Code that predates the column
neither reads nor writes it and keeps working, so this migration can run before
the new image is built. The reverse order is the unsafe one: new code reading a
column that does not exist yet would fail every query on these tables.

The CHECK constraint keeps a misspelt desk out at the database. A typo landing
in some third value would match neither desk's rules.

No index. Two values do not make a selective one, every query that will filter
on the desk also filters on the session date, and building an index would hold
a write lock on the live order table while it ran.

Revision ID: 0029_trading_desks
Revises: 0028_live_planned_risk
"""

import sqlalchemy as sa

from alembic import op

revision = "0029_trading_desks"
down_revision = "0028_live_planned_risk"
branch_labels = None
depends_on = None

_TABLES = ("paper_signals", "live_order_submissions")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("desk", sa.String(16), nullable=False, server_default="INTRADAY"))
        op.create_check_constraint(f"ck_{table}_desk", table, "desk IN ('INTRADAY', 'SWING')")


def downgrade() -> None:
    for table in _TABLES:
        op.drop_constraint(f"ck_{table}_desk", table, type_="check")
        op.drop_column(table, "desk")
