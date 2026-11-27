"""Add the research schema: datasets, their files and coverage, instruments, universe membership.

Research Lab Phase 4, approved by Shamseer on 27 November 2026 ("Phase 4
തുടങ്ങട്ടെ? Yes", after being told it needs a research schema).

Historical bars are not stored in the database. Five years of 1-minute bars for
the NIFTY 200 is about ninety million rows, roughly the size of everything the
trading database holds today several times over. They are written as one
compressed CSV per instrument and month on the research worker's volume; these
tables hold what is needed to trust and reproduce them -- where each file came
from, its SHA-256, how many bars each session has against how many it should --
and nothing about them is ever written to ``public.market_candles``.

Everything is in a separate ``research`` schema so that a database role can be
given write access to it alone (``infra/sql/research_role.sql``).

**Safe to apply under a running deployment.** A new schema and new tables;
nothing that exists is altered.

Revision ID: 0032_research_data
Revises: 0031_research_jobs
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0032_research_data"
down_revision = "0031_research_jobs"
branch_labels = None
depends_on = None

SCHEMA = "research"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.create_table(
        "instrument",
        sa.Column("instrument_key", sa.String(64), primary_key=True),
        sa.Column("symbol", sa.String(40), nullable=False),
        sa.Column("isin", sa.String(20), nullable=True),
        sa.Column("industry", sa.String(80), nullable=True),
        sa.Column("sector", sa.String(60), nullable=True),
        sa.Column("tick_size", sa.Numeric(10, 4), nullable=True),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )

    op.create_table(
        "universe_membership",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("universe", sa.String(40), nullable=False),
        sa.Column("instrument_key", sa.String(64), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("note", sa.String(255), nullable=True),
        sa.UniqueConstraint("universe", "instrument_key", "valid_from", name="uq_universe_membership"),
        schema=SCHEMA,
    )

    op.create_table(
        "dataset",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("timeframe", sa.String(20), nullable=False),
        sa.Column("price_adjustment", sa.String(30), nullable=False),
        sa.Column("benchmark_key", sa.String(64), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="BUILDING"),
        sa.Column("storage_path", sa.String(255), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("instrument_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("row_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )

    op.create_table(
        "dataset_file",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.dataset.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("instrument_key", sa.String(64), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("relative_path", sa.String(255), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("first_bar_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_bar_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("dataset_id", "instrument_key", "month", name="uq_dataset_file_month"),
        schema=SCHEMA,
    )

    op.create_table(
        "dataset_coverage",
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.dataset.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("instrument_key", sa.String(64), primary_key=True),
        sa.Column("session_date", sa.Date(), primary_key=True),
        sa.Column("bars", sa.Integer(), nullable=False),
        sa.Column("expected_bars", sa.Integer(), nullable=False),
        sa.Column("missing_bars", sa.Integer(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("bad_ohlc", sa.Integer(), nullable=False),
        sa.Column("zero_volume", sa.Integer(), nullable=False),
        sa.Column("outside_session", sa.Integer(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_dataset_coverage_dataset_session", "dataset_coverage", ["dataset_id", "session_date"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_index("ix_dataset_coverage_dataset_session", table_name="dataset_coverage", schema=SCHEMA)
    op.drop_table("dataset_coverage", schema=SCHEMA)
    op.drop_table("dataset_file", schema=SCHEMA)
    op.drop_table("dataset", schema=SCHEMA)
    op.drop_table("universe_membership", schema=SCHEMA)
    op.drop_table("instrument", schema=SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
