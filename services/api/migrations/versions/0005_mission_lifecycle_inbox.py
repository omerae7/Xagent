"""Routine lifecycle, scoped missions, mission actions, owner inbox and run de-duplication.

Revision ID: 0005
Revises: 0004

Additive only: existing routines keep running with unchanged semantics (status derived from the
old enabled/paused flags). Downgrade drops the new tables/columns; inbox items, mission grants and
per-routine counters created after the upgrade are lost on downgrade (they are recoverable only from
the pre-update backup that `xagent update` takes automatically).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_EMPTY = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "missions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("created_by_run", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("scope", postgresql.JSONB(), nullable=False, server_default=_JSON_EMPTY),
        sa.Column("max_cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("max_runs", sa.Integer(), nullable=True),
        sa.Column("max_actions", sa.Integer(), nullable=True),
        sa.Column("actions_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(64), nullable=True),
    )
    op.create_index("ix_missions_status", "missions", ["status"])

    op.create_table(
        "mission_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("mission_id", sa.Uuid(), nullable=True),
        sa.Column("routine_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target", sa.String(160), nullable=False),
        sa.Column("ref", sa.String(64), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="sent"),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default=_JSON_EMPTY),
        sa.UniqueConstraint("dedupe_key", name="uq_mission_actions_dedupe_key"),
    )
    for col in ("ts", "mission_id", "routine_id", "run_id"):
        op.create_index(f"ix_mission_actions_{col}", "mission_actions", [col])

    op.create_table(
        "inbox_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("severity", sa.String(12), nullable=False, server_default="info"),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("routine_id", sa.Uuid(), nullable=True),
        sa.Column("mission_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("link", sa.String(256), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=False, server_default=_JSON_EMPTY),
        sa.Column("delivery", postgresql.JSONB(), nullable=False, server_default=_JSON_EMPTY),
        sa.Column("read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("dedupe_key", name="uq_inbox_items_dedupe_key"),
    )
    for col in ("ts", "kind", "run_id", "routine_id", "read", "archived"):
        op.create_index(f"ix_inbox_items_{col}", "inbox_items", [col])

    add = op.add_column
    add("routines", sa.Column("status", sa.String(16), nullable=False, server_default="active"))
    add("routines", sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True))
    add("routines", sa.Column("end_at", sa.DateTime(timezone=True), nullable=True))
    add("routines", sa.Column("max_runs", sa.Integer(), nullable=True))
    for c in ("success_count", "failure_count", "no_change_count", "skipped_count"):
        add("routines", sa.Column(c, sa.Integer(), nullable=False, server_default="0"))
    add("routines", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    add("routines", sa.Column("completion_reason", sa.String(32), nullable=True))
    add("routines", sa.Column("last_error", sa.Text(), nullable=True))
    add("routines", sa.Column("budget_usd", sa.Numeric(10, 4), nullable=True))
    add("routines", sa.Column("spent_usd", sa.Numeric(14, 6), nullable=False, server_default="0"))
    add("routines", sa.Column("watch", postgresql.JSONB(), nullable=False, server_default=_JSON_EMPTY))
    add("routines", sa.Column("watch_state", postgresql.JSONB(), nullable=False, server_default=_JSON_EMPTY))
    add("routines", sa.Column("mission_id", sa.Uuid(), nullable=True))
    add("routines", sa.Column("report_policy", sa.String(16), nullable=False, server_default="important"))
    op.create_foreign_key("fk_routines_mission_id_missions", "routines", "missions", ["mission_id"], ["id"],
                          ondelete="SET NULL")
    op.create_index("ix_routines_status", "routines", ["status"])
    op.create_index("ix_routines_mission_id", "routines", ["mission_id"])
    # Preserve existing semantics: old flags map onto the new lifecycle without changing behaviour.
    op.execute("UPDATE routines SET status = 'paused' WHERE paused")
    op.execute("UPDATE routines SET status = 'completed', completion_reason = 'once', completed_at = updated_at "
               "WHERE NOT enabled AND trigger_type = 'once' AND last_status = 'success'")
    op.execute("UPDATE routines SET status = 'disabled' WHERE NOT enabled AND status = 'active'")
    op.execute("UPDATE routines SET starts_at = created_at WHERE starts_at IS NULL")

    add("runs", sa.Column("dedupe_key", sa.String(160), nullable=True))
    add("runs", sa.Column("outcome", sa.String(16), nullable=True))
    op.create_unique_constraint("uq_runs_dedupe_key", "runs", ["dedupe_key"])


def downgrade() -> None:
    op.drop_constraint("uq_runs_dedupe_key", "runs", type_="unique")
    op.drop_column("runs", "outcome")
    op.drop_column("runs", "dedupe_key")
    # Restore the old flags from the lifecycle before dropping it, so a downgraded scheduler
    # never restarts a mission that had already finished.
    op.execute("UPDATE routines SET enabled = false WHERE status IN "
               "('completed', 'failed', 'cancelled', 'expired', 'disabled')")
    op.execute("UPDATE routines SET paused = true WHERE status = 'paused'")
    op.drop_index("ix_routines_mission_id", table_name="routines")
    op.drop_index("ix_routines_status", table_name="routines")
    op.drop_constraint("fk_routines_mission_id_missions", "routines", type_="foreignkey")
    for c in ("report_policy", "mission_id", "watch_state", "watch", "spent_usd", "budget_usd", "last_error",
              "completion_reason", "completed_at", "skipped_count", "no_change_count", "failure_count",
              "success_count", "max_runs", "end_at", "starts_at", "status"):
        op.drop_column("routines", c)
    op.drop_table("inbox_items")
    op.drop_table("mission_actions")
    op.drop_table("missions")
