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
    # Restore archived private records after upgrading back from 0002.
    # This migration runs in a transaction; archive is dropped only after insert.
    for table in ("semantic_facts", "episodes", "experiences"):
        archive = f"xagent_private_{table}_archive"
        conn = op.get_bind()
        exists = conn.execute(sa.text("SELECT to_regclass(:archive)"), {"archive": f"public.{archive}"}).scalar()
        if exists is None:
            continue
        names = [col["name"] for col in sa.inspect(conn).get_columns(table) if not col.get("computed")]
        quote = conn.dialect.identifier_preparer.quote
        cols = ", ".join(quote(name) for name in names)
        op.execute(sa.text(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {archive}"))
        op.execute(sa.text(f"DROP TABLE {archive}"))


def downgrade() -> None:
    for col in ("created_at", "op", "run_id", "status", "host_job"):
        op.drop_index(op.f(f"ix_host_ops_{col}"), table_name="host_ops")
    op.drop_table("host_ops")
    op.drop_column("usage_events", "cost_estimated")
    op.drop_constraint("uq_semantic_facts_scope_section_key", "semantic_facts", type_="unique")
    # A rollback to 0002 must not expose any private row as global.
    # Archive exact typed rows BEFORE dropping the scope column; on upgrading
    # back to 0003 they are restored losslessly, not merely marked "purged".
    for table in ("semantic_facts", "episodes", "experiences"):
        archive = f"xagent_private_{table}_archive"
        op.execute(sa.text(f"CREATE TABLE IF NOT EXISTS {archive} AS SELECT * FROM {table} WHERE false"))
        op.execute(sa.text(f"INSERT INTO {archive} SELECT * FROM {table} WHERE scope <> 'global'"))
        if table == "episodes":
            # The immutable episode trigger requires a controlled migration-only
            # bypass; archived rows are kept and restored on the next upgrade.
            op.execute("ALTER TABLE episodes DISABLE TRIGGER episodes_no_delete")
        op.execute(sa.text(f"DELETE FROM {table} WHERE scope <> 'global'"))
        if table == "episodes":
            op.execute("ALTER TABLE episodes ENABLE TRIGGER episodes_no_delete")
    op.create_unique_constraint("uq_semantic_facts_section", "semantic_facts", ["section", "key"])
    for table in ("semantic_facts", "episodes", "experiences"):
        op.drop_index(f"ix_{table}_scope", table_name=table)
        op.drop_column(table, "scope")
