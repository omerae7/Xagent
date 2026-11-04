"""Durable provider attempts and unique Dream slots.

Revision ID: 0004
Revises: 0003
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("usage_events", sa.Column("attempt_status", sa.String(24), nullable=False, server_default="confirmed"))
    op.add_column("usage_events", sa.Column("request_id", sa.String(64), nullable=True))
    op.add_column("usage_events", sa.Column("provider_request_id", sa.String(160), nullable=True))
    op.add_column("usage_events", sa.Column("usage_details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("usage_events", sa.Column("reserved_cost_usd", sa.Numeric(14, 6), nullable=False, server_default="0"))
    op.add_column("usage_events", sa.Column("reserved_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("usage_events", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("uq_usage_events_request_id", "usage_events", ["request_id"])
    op.add_column("dream_reports", sa.Column("slot_key", sa.String(40), nullable=True))
    op.add_column("dream_reports", sa.Column("trigger", sa.String(16), nullable=False, server_default="scheduled"))
    op.create_unique_constraint("uq_dream_reports_slot_key", "dream_reports", ["slot_key"])
    # Safe opt-in: previous active schedules cannot restart billing on upgrade.
    op.execute("UPDATE settings_kv SET value = jsonb_set(value, '{enabled}', 'false'::jsonb, true) "
               "WHERE key = 'dream'")


def downgrade() -> None:
    op.drop_constraint("uq_dream_reports_slot_key", "dream_reports", type_="unique")
    op.drop_column("dream_reports", "trigger")
    op.drop_column("dream_reports", "slot_key")
    op.drop_constraint("uq_usage_events_request_id", "usage_events", type_="unique")
    for field in ("finished_at", "reserved_tokens", "reserved_cost_usd", "usage_details",
                  "provider_request_id", "request_id", "attempt_status"):
        op.drop_column("usage_events", field)
