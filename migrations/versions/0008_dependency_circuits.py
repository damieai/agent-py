"""Tenant-scoped shared circuit state for enterprise reads."""

import sqlalchemy as sa
from alembic import op

revision = "0008_dependency_circuits"
down_revision = "0007_worker_admission"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dependency_circuits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dependency", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_token", sa.String(36), nullable=True),
        sa.UniqueConstraint("tenant_id", "dependency"),
    )
    op.create_index("ix_dependency_circuits_tenant_id", "dependency_circuits", ["tenant_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE dependency_circuits ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE dependency_circuits FORCE ROW LEVEL SECURITY")
        op.execute("""CREATE POLICY tenant_isolation ON dependency_circuits
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")


def downgrade():
    op.drop_table("dependency_circuits")
