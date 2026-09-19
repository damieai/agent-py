"""Shared tenant admission lock and expiring worker tickets."""

import sqlalchemy as sa
from alembic import op

revision = "0007_worker_admission"
down_revision = "0006_repair_runs"
branch_labels = None
depends_on = None


def base():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table(
        "admission_gates",
        *base(),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("max_active", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_table(
        "work_leases",
        *base(),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("owner_token", sa.String(36), nullable=True),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "task_id"),
    )
    for table in ("admission_gates", "work_leases"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        if op.get_bind().dialect.name == "postgresql":
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            op.execute(f'''CREATE POLICY tenant_isolation ON "{table}"
                USING (tenant_id = current_setting('app.tenant_id', true))
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true))''')


def downgrade():
    op.drop_table("work_leases")
    op.drop_table("admission_gates")
