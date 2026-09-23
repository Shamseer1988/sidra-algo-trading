"""Let a halt say which day it stopped: paper's or the broker's.

0023 recorded the paper day's stop. The same two numbers now govern live
submission, and the two have to halt independently: paper P&L comes from the
simulated ledger and live P&L from the broker, so a paper day that hits its
target says nothing about the money in the Upstox account. Stopping real
trading on the strength of a simulation would be a refusal nobody could explain.

The table is renamed rather than duplicated, because one concept with two scopes
is not two concepts. Existing rows are paper by construction — nothing else
could have written one — so the backfill is the server default rather than a
guess.

Revision ID: 0024_session_halt_mode
Revises: 0023_paper_session_halt
"""

import sqlalchemy as sa

from alembic import op

revision = "0024_session_halt_mode"
down_revision = "0023_paper_session_halt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("paper_session_halts", "session_halts")
    op.add_column(
        "session_halts",
        sa.Column("mode", sa.String(length=10), nullable=False, server_default="PAPER"),
    )
    # The old constraint allowed one halt per date; there are now two scopes.
    op.drop_constraint("uq_paper_session_halts_session_date", "session_halts", type_="unique")
    op.drop_index("ix_paper_session_halts_session_date", table_name="session_halts")
    op.drop_index("ix_paper_session_halts_created_at", table_name="session_halts")
    op.create_unique_constraint("uq_session_halts_session_date_mode", "session_halts", ["session_date", "mode"])
    op.create_index("ix_session_halts_session_date", "session_halts", ["session_date"])
    op.create_index("ix_session_halts_mode", "session_halts", ["mode"])
    op.create_index("ix_session_halts_created_at", "session_halts", ["created_at"])


def downgrade() -> None:
    # Live halts have no home in the old shape, and a paper halt invented from
    # one would stop a paper day that never reached its limit.
    op.execute("delete from session_halts where mode <> 'PAPER'")
    op.drop_index("ix_session_halts_created_at", table_name="session_halts")
    op.drop_index("ix_session_halts_mode", table_name="session_halts")
    op.drop_index("ix_session_halts_session_date", table_name="session_halts")
    op.drop_constraint("uq_session_halts_session_date_mode", "session_halts", type_="unique")
    op.drop_column("session_halts", "mode")
    op.create_unique_constraint("uq_paper_session_halts_session_date", "session_halts", ["session_date"])
    op.create_index("ix_paper_session_halts_session_date", "session_halts", ["session_date"], unique=True)
    op.create_index("ix_paper_session_halts_created_at", "session_halts", ["created_at"])
    op.rename_table("session_halts", "paper_session_halts")
