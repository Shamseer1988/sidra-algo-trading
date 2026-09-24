"""Keep every saved version of a settings row.

application_settings holds the current value and a row-level updated_at, which
cannot answer either question that matters. "When did I last change the daily
loss stop" is about one key, not the row. And a trade has to be explicable by
the settings that produced it, which needs the configuration as it stood then,
not as it stands now.

Append-only by intent: nothing updates or deletes a revision. A settings history
that can be edited is not a history.

No backfill. The current row's value is the present, not a past decision, and
inventing a revision for it would put a change in the record that nobody made.

Revision ID: 0025_setting_revisions
Revises: 0024_session_halt_mode
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0025_setting_revisions"
down_revision = "0024_session_halt_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "setting_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("changed_keys", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("risk_increased", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "changed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_setting_revisions_key", "setting_revisions", ["key"])
    op.create_index("ix_setting_revisions_created_at", "setting_revisions", ["created_at"])
    op.create_index("ix_setting_revisions_key_created", "setting_revisions", ["key", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_setting_revisions_key_created", table_name="setting_revisions")
    op.drop_index("ix_setting_revisions_created_at", table_name="setting_revisions")
    op.drop_index("ix_setting_revisions_key", table_name="setting_revisions")
    op.drop_table("setting_revisions")
