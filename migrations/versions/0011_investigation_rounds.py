"""Frozen inputs for bounded, read-only investigation rounds."""

import sqlalchemy as sa
from alembic import op

revision = "0011_investigation_rounds"
down_revision = "0010_dependency_bulkhead"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "investigation_rounds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("query", sa.String(8000), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.UniqueConstraint("tenant_id", "task_id", "ordinal"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.id"],
            name="fk_investigation_rounds_task_id_tenant",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_index("ix_investigation_rounds_tenant_id", "investigation_rounds", ["tenant_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE investigation_rounds ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE investigation_rounds FORCE ROW LEVEL SECURITY")
        op.execute("""CREATE POLICY tenant_isolation ON investigation_rounds
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")


def downgrade():
    op.drop_table("investigation_rounds")
