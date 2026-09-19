"""Force PostgreSQL tenant isolation; SQLite remains development-only."""

from alembic import op

revision = "0002_tenant_rls"
down_revision = "4fbbd54df2ef"
branch_labels = None
depends_on = None
TABLES = (
    "tasks",
    "operations",
    "approvals",
    "task_events",
    "outbox",
    "inbox",
    "artifacts",
    "grants",
    "reservations",
    "daily_budgets",
    "documents",
    "policies",
)


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'''CREATE POLICY tenant_isolation ON "{table}"
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))''')


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.execute(f'DROP POLICY tenant_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
