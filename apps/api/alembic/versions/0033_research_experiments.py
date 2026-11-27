"""Add pre-registered research experiments: hypotheses, experiments and their results.

Research Lab Phase 5, approved by Shamseer on 27 November 2026 ("start above
pending all", the pending list including Phase 5 and its research entities).

An experiment is written down before it is run -- the hypothesis, the dataset,
the parameter grid and its size, the selection period, the sealed holdout
period, the metric and the threshold that would count as success -- and
cannot be edited once queued. The selection job never loads the holdout's
bars; the holdout is scored once, for the parameters chosen on the selection
period, and the experiment records that it has been used.

**Safe to apply under a running deployment.** New tables in the ``research``
schema only.

Revision ID: 0033_research_experiments
Revises: 0032_research_data
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0033_research_experiments"
down_revision = "0032_research_data"
branch_labels = None
depends_on = None

SCHEMA = "research"


def upgrade() -> None:
    op.create_table(
        "hypothesis",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="OPEN"),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "experiment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "hypothesis_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.hypothesis.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.dataset.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("instruments", sa.JSON(), nullable=False),
        sa.Column("strategy", sa.JSON(), nullable=False),
        sa.Column("controls", sa.JSON(), nullable=False),
        sa.Column("execution", sa.JSON(), nullable=False),
        sa.Column("parameter_grid", sa.JSON(), nullable=False),
        sa.Column("variants", sa.Integer(), nullable=False),
        sa.Column("selection_start", sa.Date(), nullable=False),
        sa.Column("selection_end", sa.Date(), nullable=False),
        sa.Column("holdout_start", sa.Date(), nullable=False),
        sa.Column("holdout_end", sa.Date(), nullable=False),
        sa.Column("train_sessions", sa.Integer(), nullable=False),
        sa.Column("test_sessions", sa.Integer(), nullable=False),
        sa.Column("success_mean_r", sa.Numeric(8, 4), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        sa.Column("chosen_index", sa.Integer(), nullable=True),
        sa.Column("chosen_parameters", sa.JSON(), nullable=True),
        sa.Column("holdout_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("verdict", sa.String(20), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "experiment_result",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "experiment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.experiment.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("code_fingerprint", sa.String(64), nullable=False),
        sa.Column("dataset_sha256", sa.String(64), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("experiment_id", "kind", name="uq_experiment_result_kind"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("experiment_result", schema=SCHEMA)
    op.drop_table("experiment", schema=SCHEMA)
    op.drop_table("hypothesis", schema=SCHEMA)
