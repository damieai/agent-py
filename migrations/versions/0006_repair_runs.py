"""Persist bounded repair campaigns and attempts, with forced tenant isolation."""

import sqlalchemy as sa
from alembic import op

revision = "0006_repair_runs"
down_revision = "0005_evidence_task_scope"
branch_labels = None
depends_on = None


def base_columns():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table(
        "repair_runs",
        *base_columns(),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("config_digest", sa.String(64), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.UniqueConstraint("tenant_id", "task_id"),
    )
    op.create_table(
        "repair_attempts",
        *base_columns(),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("proposal", sa.JSON(), nullable=True),
        sa.Column("patch_artifact_id", sa.String(36), nullable=True),
        sa.Column("verification_artifact_id", sa.String(36), nullable=True),
        sa.Column("outcome", sa.String(40), nullable=True),
        sa.Column("verification_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_token", sa.String(36), nullable=True),
        sa.Column("verification_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id", "run_id", "ordinal"),
    )
    for table in ("repair_runs", "repair_attempts"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        if op.get_bind().dialect.name == "postgresql":
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            op.execute(f'''CREATE POLICY tenant_isolation ON "{table}"
                USING (tenant_id = current_setting('app.tenant_id', true))
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true))''')


def downgrade():
    op.drop_table("repair_attempts")
    op.drop_table("repair_runs")
