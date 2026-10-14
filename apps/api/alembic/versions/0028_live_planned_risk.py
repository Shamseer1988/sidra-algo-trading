"""Record what a live order was sized to risk, on the live order's own row.

The live risk engine gained the three portfolio limits that until now existed
only in the paper risk engine: concurrent positions, open exposure, and the
day's risk allocation. Real money was being bounded by a simulation -- the two
agree while nothing diverges and are unrelated the moment anything does.

Positions and exposure come from the broker's position book. The day's
allocation has no broker equivalent: it is the sum of what each of the day's
orders was *sized to risk*, which only this system knows. The first
implementation read it by joining ``live_order_submissions`` to
``paper_signals.risk_amount``, and that is the mistake this column exists to
avoid. The standing rule in this codebase is that the live path shares no risk
source with paper, and a join into a paper table would make the live account's
real budget depend on a simulation's record -- enforced by the boundary test in
``test_submission_boundary``, which fails on the import.

So the number is written on the live row at the moment the order is prepared,
one line before the request leaves: entry minus stop, times quantity, as the
strategy planned it.

Nullable, and deliberately so in three cases. An exit and a protective stop
risk nothing new and carry null rather than repeating the entry's risk. A
manually placed order that cannot say what it risks carries null rather than a
guess. And every row written before this migration carries null, which the
allocation counts as nothing -- the alternative, a default of zero, would read
as "this order risked nothing" and is the same claim by another name.

Revision ID: 0028_live_planned_risk
Revises: 0027_live_submission_fills
"""

import sqlalchemy as sa

from alembic import op

revision = "0028_live_planned_risk"
down_revision = "0027_live_submission_fills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("live_order_submissions", sa.Column("planned_risk", sa.Numeric(18, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("live_order_submissions", "planned_risk")
