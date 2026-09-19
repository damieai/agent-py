"""Bind inference identity to its request and persist its validated decision."""

import sqlalchemy as sa
from alembic import op

revision = "0004_inference_result"
down_revision = "0003_inference_dispatch"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reservations", sa.Column("request_digest", sa.String(64), nullable=True))
    op.add_column("reservations", sa.Column("decision", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("reservations", "decision")
    op.drop_column("reservations", "request_digest")
