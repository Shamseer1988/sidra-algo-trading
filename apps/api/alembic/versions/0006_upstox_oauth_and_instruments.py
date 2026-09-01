"""add encrypted Upstox credential and instrument refresh records

Revision ID: 0006_upstox_oauth_and_instruments
Revises: 0005_paper_strategy_signals
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_upstox_oauth_and_instruments"
down_revision = "0005_paper_strategy_signals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "broker_credentials",
        sa.Column("provider", sa.String(length=40), primary_key=True),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_broker_credentials_expires_at", "broker_credentials", ["expires_at"])
    op.create_table(
        "instrument_master_refreshes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("instrument_count", sa.Integer(), nullable=False),
        sa.Column("configured_keys", sa.JSON(), nullable=False),
        sa.Column("missing_keys", sa.JSON(), nullable=False),
    )
    op.create_index("ix_instrument_master_refreshes_provider", "instrument_master_refreshes", ["provider"])
    op.create_index(
        "ix_instrument_master_refreshes_provider_fetched", "instrument_master_refreshes", ["provider", "fetched_at"]
    )


def downgrade() -> None:
    op.drop_table("instrument_master_refreshes")
    op.drop_table("broker_credentials")
