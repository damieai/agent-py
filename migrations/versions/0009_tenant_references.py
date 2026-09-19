"""Enforce tenant-scoped relational integrity without cascading audit deletion."""

import sqlalchemy as sa
from alembic import op

revision = "0009_tenant_references"
down_revision = "0008_dependency_circuits"
branch_labels = None
depends_on = None

# Frozen migration contract: do not import evolving application metadata.
PARENTS = ("tasks", "operations", "artifacts", "repair_runs")
REFERENCES = (
    ("operations", "task_id", "tasks"),
    ("approvals", "operation_id", "operations"),
    ("task_events", "task_id", "tasks"),
    ("outbox", "task_id", "tasks"),
    ("artifacts", "task_id", "tasks"),
    ("reservations", "task_id", "tasks"),
    ("documents", "task_id", "tasks"),
    ("repair_runs", "task_id", "tasks"),
    ("repair_runs", "snapshot_id", "artifacts"),
    ("repair_attempts", "run_id", "repair_runs"),
    ("repair_attempts", "patch_artifact_id", "artifacts"),
    ("repair_attempts", "verification_artifact_id", "artifacts"),
    ("work_leases", "task_id", "tasks"),
)
TABLES = tuple(dict.fromkeys((*PARENTS, *(r[0] for r in REFERENCES))))


def lock_and_check_visibility():
    if op.get_context().as_sql:
        raise RuntimeError("Tenant reference migration requires online data validation")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        if not connection.scalar(
            sa.text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
        ):
            raise RuntimeError(
                "Tenant reference migration requires an administrative BYPASSRLS role"
            )
        connection.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
        connection.execute(
            sa.text("LOCK TABLE " + ", ".join(sorted(TABLES)) + " IN SHARE ROW EXCLUSIVE MODE")
        )


def upgrade():
    lock_and_check_visibility()
    connection = op.get_bind()
    invalid = []
    for table, column, parent in REFERENCES:
        count = connection.scalar(
            sa.text(
                f"SELECT count(*) FROM {table} c LEFT JOIN {parent} p "
                f"ON c.tenant_id=p.tenant_id AND c.{column}=p.id "
                f"WHERE c.{column} IS NOT NULL AND p.id IS NULL"
            )
        )
        if count:
            invalid.append(f"{table}.{column}: {count}")
    if invalid:
        # Counts only: migration logs must not leak tenant IDs or business data.
        raise RuntimeError(
            "Invalid tenant references; repair data before retry: " + ", ".join(invalid)
        )
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            if table in PARENTS:
                batch.create_unique_constraint(f"uq_{table}_tenant_id_id", ["tenant_id", "id"])
            for child, column, parent in REFERENCES:
                if child == table:
                    batch.create_foreign_key(
                        f"fk_{table}_{column}_tenant",
                        parent,
                        ["tenant_id", column],
                        ["tenant_id", "id"],
                        deferrable=True,
                        initially="DEFERRED",
                    )


def downgrade():
    lock_and_check_visibility()
    # Remove all references before their supporting unique keys.
    for table in reversed(TABLES):
        with op.batch_alter_table(table) as batch:
            for child, column, _ in REFERENCES:
                if child == table:
                    batch.drop_constraint(f"fk_{table}_{column}_tenant", type_="foreignkey")
    for table in reversed(PARENTS):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"uq_{table}_tenant_id_id", type_="unique")
