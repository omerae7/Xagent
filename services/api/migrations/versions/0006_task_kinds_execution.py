"""Task kinds (human / agent / dependent) and deterministic Task↔Run linkage.

Revision ID: 0006
Revises: 0005

Additive only. Every existing task becomes `human` (follow-up/reminder only), so no task that was created
before this release can start executing automatically. Downgrade drops the new columns; tasks keep their
title/status/dates, but the kind/dependency/result fields written after the upgrade are lost (recoverable
only from the pre-update backup).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("kind", sa.String(12), nullable=False, server_default="human"))
    op.add_column("tasks", sa.Column("depends_on_id", sa.Uuid(), nullable=True))
    op.add_column("tasks", sa.Column("missed_policy", sa.String(12), nullable=False, server_default="run_late"))
    op.add_column("tasks", sa.Column("last_run_id", sa.Uuid(), nullable=True))
    op.add_column("tasks", sa.Column("result_summary", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key("fk_tasks_depends_on_id_tasks", "tasks", "tasks", ["depends_on_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_tasks_kind", "tasks", ["kind"])


def downgrade() -> None:
    # وضعیت جدید failed در نسخه قبلی شناخته نمی‌شود؛ به waiting برمی‌گردد تا داده نامعتبر نماند
    op.execute("UPDATE tasks SET status = 'waiting' WHERE status = 'failed'")
    op.drop_index("ix_tasks_kind", table_name="tasks")
    op.drop_constraint("fk_tasks_depends_on_id_tasks", "tasks", type_="foreignkey")
    for c in ("reminded_at", "result_summary", "last_run_id", "missed_policy", "depends_on_id", "kind"):
        op.drop_column("tasks", c)
