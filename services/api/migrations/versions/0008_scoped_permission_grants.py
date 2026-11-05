"""Add opt-in scoped permissions. No existing permissions or rows are changed.

Downgrade removes these new tables and their permission history. For an ordinary
application rollback retain this additive schema; export before explicit downgrade.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("permission_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("employee_id", sa.Uuid()),
        sa.Column("source_approval_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("mission_key", sa.String(64)),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("category", sa.String(48), nullable=False),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("scope", JSONB(), nullable=False, server_default="{}"),
        sa.Column("policy_revision", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_permission_grants_owner", "permission_grants", ["owner"])
    op.create_index("ix_permission_grants_scope_hash", "permission_grants", ["scope_hash"])
    op.create_table("permission_uses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("grant_id", sa.Uuid(), sa.ForeignKey("permission_grants.id"), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_call_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("grant_id", "run_id", "tool_call_id", name="uq_permission_use"))
    op.create_index("ix_permission_uses_grant_id", "permission_uses", ["grant_id"])


def downgrade():
    op.drop_table("permission_uses")
    op.drop_table("permission_grants")
