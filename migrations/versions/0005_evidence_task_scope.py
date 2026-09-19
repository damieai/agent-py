"""Bind newly collected evidence to its originating task."""

import sqlalchemy as sa
from alembic import op

revision = "0005_evidence_task_scope"
down_revision = "0004_inference_result"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("task_id", sa.String(36), nullable=True))


def downgrade():
    op.drop_column("documents", "task_id")
