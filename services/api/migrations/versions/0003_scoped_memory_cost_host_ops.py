"""scoped employee memory, estimated cost flag, host ops audit

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # حافظه دارای فضای نام (ایزوله‌بودن حافظه خصوصی کارمند)
    for table in ("semantic_facts", "episodes", "experiences"):
        op.add_column(table, sa.Column("scope", sa.String(length=64), server_default="global", nullable=False))
        op.create_index(f"ix_{table}_scope", table, ["scope"], unique=False)
    op.drop_constraint("uq_semantic_facts_section", "semantic_facts", type_="unique")
    op.create_unique_constraint("uq_semantic_facts_scope_section_key", "semantic_facts", ["scope", "section", "key"])
    # هزینه برآوردی مدل بدون قیمت
    op.add_column("usage_events", sa.Column("cost_estimated", sa.Boolean(), server_default="false", nullable=False))
    # حسابرسی عملیات میزبان
    op.create_table(
        "host_ops",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("op", sa.String(length=64), nullable=False),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.String(length=64), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("approved_by", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("host_job", sa.String(length=64), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column("output_bytes", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_host_ops")),
        sa.UniqueConstraint("approval_id", name=op.f("uq_host_ops_approval_id")),
    )
    for col in ("created_at", "op", "run_id", "status", "host_job"):
        op.create_index(op.f(f"ix_host_ops_{col}"), "host_ops", [col], unique=False)


def downgrade() -> None:
    for col in ("created_at", "op", "run_id", "status", "host_job"):
        op.drop_index(op.f(f"ix_host_ops_{col}"), table_name="host_ops")
    op.drop_table("host_ops")
    op.drop_column("usage_events", "cost_estimated")
    op.drop_constraint("uq_semantic_facts_scope_section_key", "semantic_facts", type_="unique")
    # حافظه خصوصی حذف نمی‌شود ولی در نسخه قدیمی (بدون فضای نام) هم در دسترس مدیر قرار نمی‌گیرد
    op.execute("UPDATE semantic_facts SET status = 'purged', key = left('private:' || md5(id::text) || ':' || key, 128) "
               "WHERE scope <> 'global'")
    op.execute("UPDATE experiences SET status = 'purged' WHERE scope <> 'global'")
    op.create_unique_constraint("uq_semantic_facts_section", "semantic_facts", ["section", "key"])
    for table in ("semantic_facts", "episodes", "experiences"):
        op.drop_index(f"ix_{table}_scope", table_name=table)
        op.drop_column(table, "scope")
