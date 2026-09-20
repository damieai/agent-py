"""Tenant-scoped telemetry links, independent of workflow recovery authority."""

import sqlalchemy as sa
from alembic import op

revision = "0012_task_traces"
down_revision = "0011_investigation_rounds"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_traces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("origin", sa.String(55), nullable=True),
        sa.Column("dispatch", sa.String(55), nullable=True),
        sa.Column("latest", sa.String(55), nullable=True),
        sa.UniqueConstraint("tenant_id", "task_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.id"],
            name="fk_task_traces_task_id_tenant",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_index("ix_task_traces_tenant_id", "task_traces", ["tenant_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE task_traces ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE task_traces FORCE ROW LEVEL SECURITY")
        op.execute("""CREATE POLICY tenant_isolation ON task_traces
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")


def downgrade():
    op.drop_table("task_traces")
