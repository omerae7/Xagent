"""External agent executor runs (Cursor Cloud Agents).

Revision ID: 0007
Revises: 0006

Additive only: a new table, no change to existing rows. Downgrade drops the table, which loses the local
record of delegated Cursor agents (agent ids, PR links, results). The remote agents themselves are not
affected and remain visible in the Cursor dashboard; export the table before downgrading if it is needed.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_agent_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("executor", sa.String(24), nullable=False, server_default="cursor"),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=False, unique=True),
        sa.Column("agent_id", sa.String(64), nullable=False, unique=True),
        sa.Column("remote_run_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column("remote_status", sa.String(32), nullable=True),
        sa.Column("repo_url", sa.String(300), nullable=False),
        sa.Column("starting_ref", sa.String(120), nullable=True),
        sa.Column("branch", sa.String(200), nullable=True),
        sa.Column("pr_url", sa.String(400), nullable=True),
        sa.Column("agent_url", sa.String(400), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(80), nullable=True),
        sa.Column("auto_create_pr", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("poll_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cost_status", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
    )
    op.create_index("ix_external_agent_runs_created_at", "external_agent_runs", ["created_at"])
    op.create_index("ix_external_agent_runs_run_id", "external_agent_runs", ["run_id"])
    op.create_index("ix_external_agent_runs_status", "external_agent_runs", ["status"])
    op.create_index("ix_external_agent_runs_next_poll_at", "external_agent_runs", ["next_poll_at"])


def downgrade() -> None:
    op.drop_table("external_agent_runs")
