"""Prevent retries from dispatching an already reserved model call twice."""

import sqlalchemy as sa
from alembic import op

revision = "0003_inference_dispatch"
down_revision = "0002_tenant_rls"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "reservations",
        sa.Column("dispatched", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("reservations", "dispatched")
