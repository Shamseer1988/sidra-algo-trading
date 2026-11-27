"""Add the research job queue: jobs and their append-only event history.

Approved by Shamseer on 27 November 2026 as the one migration Research Lab
Phase 3 needs ("Phase 3 (research-worker) തുടങ്ങട്ടെ? yes", after being told
it needs a job table).

Backtests and sweeps used to run inside the API process, in a thread. A sweep
is pure-Python CPU for an hour, and a thread holding the GIL slows everything
else in that process -- including the scheduler job that squares live
positions off and the one that reconciles them. ``research_jobs`` lets the API
hand the work to a separate ``research-worker`` container and only wait for the
answer.

``research_job_events`` is the audit trail: every state change, with who or
what made it. Append-only by intent.

**Safe to apply under a running deployment.** Two new tables; nothing that
exists is altered, so code that predates them neither notices nor cares.

Revision ID: 0031_research_jobs
Revises: 0030_swing_paper_ledger
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0031_research_jobs"
down_revision = "0030_swing_paper_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("failure_detail", sa.String(500), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="7200"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("worker_id", sa.String(80), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_research_jobs_status_priority", "research_jobs", ["status", "priority", "created_at"])
    op.create_index("ix_research_jobs_created_at", "research_jobs", ["created_at"])

    op.create_table(
        "research_job_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(20), nullable=True),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_research_job_events_job_occurred", "research_job_events", ["job_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_research_job_events_job_occurred", table_name="research_job_events")
    op.drop_table("research_job_events")
    op.drop_index("ix_research_jobs_created_at", table_name="research_jobs")
    op.drop_index("ix_research_jobs_status_priority", table_name="research_jobs")
    op.drop_table("research_jobs")
