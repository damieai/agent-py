"""Shared dependency read slots with expiring ownership, protected by tenant RLS."""

import sqlalchemy as sa
from alembic import op

revision = "0010_dependency_bulkhead"
down_revision = "0009_tenant_references"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "dependency_circuits",
        sa.Column("max_in_flight", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "dependency_read_leases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dependency", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "dependency"],
            ["dependency_circuits.tenant_id", "dependency_circuits.dependency"],
            name="fk_dependency_read_leases_circuit_tenant",
        ),
    )
    op.create_index("ix_dependency_read_leases_tenant_id", "dependency_read_leases", ["tenant_id"])
    op.create_index(
        "ix_dependency_read_leases_scope_expiry",
        "dependency_read_leases",
        ["tenant_id", "dependency", "expires_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE dependency_read_leases ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE dependency_read_leases FORCE ROW LEVEL SECURITY")
        op.execute("""CREATE POLICY tenant_isolation ON dependency_read_leases
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")


def downgrade():
    op.drop_table("dependency_read_leases")
    with op.batch_alter_table("dependency_circuits") as batch:
        batch.drop_column("max_in_flight")
