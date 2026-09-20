import contextlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def now() -> datetime:
    return datetime.now(UTC)


def uid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Record(Base):
    __abstract__ = True
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def tenant_reference(table: str, column: str, parent: str):
    """Commit-time integrity permits atomic parent/child inserts without ORM relationships."""
    return ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{parent}.tenant_id", f"{parent}.id"],
        name=f"fk_{table}_{column}_tenant",
        deferrable=True,
        initially="DEFERRED",
    )


def tenant_identity(table: str):
    return UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id")


class Task(Record):
    __tablename__ = "tasks"
    __table_args__ = (
        tenant_identity("tasks"),
        UniqueConstraint("tenant_id", "request_key"),
    )
    request_key: Mapped[str] = mapped_column(String(160))
    request_digest: Mapped[str] = mapped_column(String(64))
    principal: Mapped[str] = mapped_column(String(160))
    contract: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED")
    result: Mapped[str | None] = mapped_column(String(30), nullable=True)
    waiting_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cancelled: Mapped[bool] = mapped_column(default=False)
    taken_over: Mapped[bool] = mapped_column(default=False)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    spent: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    next_sequence: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)


class TaskTrace(Record):
    __tablename__ = "task_traces"
    __table_args__ = (
        tenant_reference("task_traces", "task_id", "tasks"),
        UniqueConstraint("tenant_id", "task_id"),
    )
    task_id: Mapped[str] = mapped_column(String(36))
    origin: Mapped[str | None] = mapped_column(String(55), nullable=True)
    dispatch: Mapped[str | None] = mapped_column(String(55), nullable=True)
    latest: Mapped[str | None] = mapped_column(String(55), nullable=True)


class Operation(Record):
    __tablename__ = "operations"
    __table_args__ = (
        tenant_identity("operations"),
        tenant_reference("operations", "task_id", "tasks"),
        UniqueConstraint("tenant_id", "task_id", "step_key"),
    )
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    step_key: Mapped[str] = mapped_column(String(160))
    tool: Mapped[str] = mapped_column(String(40))
    resource: Mapped[str] = mapped_column(String(160))
    parameters: Mapped[dict] = mapped_column(JSON)
    payload_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="NOT_SUBMITTED")
    recovery_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    error: Mapped[str | None] = mapped_column(String(160), nullable=True)


class Approval(Record):
    __tablename__ = "approvals"
    __table_args__ = (
        tenant_reference("approvals", "operation_id", "operations"),
        UniqueConstraint("tenant_id", "operation_id"),
    )
    operation_id: Mapped[str] = mapped_column(String(36))
    payload_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    decided_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TaskEvent(Record):
    __tablename__ = "task_events"
    __table_args__ = (
        tenant_reference("task_events", "task_id", "tasks"),
        UniqueConstraint("tenant_id", "task_id", "sequence"),
    )
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON)


class Outbox(Record):
    __tablename__ = "outbox"
    __table_args__ = (
        tenant_reference("outbox", "task_id", "tasks"),
        UniqueConstraint("tenant_id", "task_id"),
    )
    task_id: Mapped[str] = mapped_column(String(36))
    delivered: Mapped[bool] = mapped_column(default=False)


class Inbox(Record):
    __tablename__ = "inbox"
    __table_args__ = (UniqueConstraint("tenant_id", "provider", "external_id"),)
    provider: Mapped[str] = mapped_column(String(60))
    external_id: Mapped[str] = mapped_column(String(160))
    payload: Mapped[dict] = mapped_column(JSON)
    processed: Mapped[bool] = mapped_column(default=False)


class Artifact(Record):
    __tablename__ = "artifacts"
    __table_args__ = (
        tenant_identity("artifacts"),
        tenant_reference("artifacts", "task_id", "tasks"),
    )
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(60))
    digest: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(200))


class Grant(Record):
    __tablename__ = "grants"
    __table_args__ = (UniqueConstraint("tenant_id", "subject", "project", "environment"),)
    subject: Mapped[str] = mapped_column(String(160))
    project: Mapped[str] = mapped_column(String(80))
    environment: Mapped[str] = mapped_column(String(40))
    revoked: Mapped[bool] = mapped_column(default=False)


class Reservation(Record):
    __tablename__ = "reservations"
    __table_args__ = (
        tenant_reference("reservations", "task_id", "tasks"),
        UniqueConstraint("tenant_id", "task_id", "call_key"),
    )
    task_id: Mapped[str] = mapped_column(String(36))
    call_key: Mapped[str] = mapped_column(String(160))
    maximum: Mapped[int] = mapped_column(Integer)
    actual: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dispatched: Mapped[bool] = mapped_column(default=False)
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    day: Mapped[str] = mapped_column(String(10), default=lambda: now().date().isoformat())


class InvestigationRound(Record):
    __tablename__ = "investigation_rounds"
    __table_args__ = (
        tenant_reference("investigation_rounds", "task_id", "tasks"),
        UniqueConstraint("tenant_id", "task_id", "ordinal"),
    )
    task_id: Mapped[str] = mapped_column(String(36))
    ordinal: Mapped[int] = mapped_column(Integer)
    query: Mapped[str] = mapped_column(String(8000))
    context: Mapped[dict] = mapped_column(JSON)


class DailyBudget(Record):
    __tablename__ = "daily_budgets"
    __table_args__ = (UniqueConstraint("tenant_id", "day"),)
    day: Mapped[str] = mapped_column(String(10))
    spent: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)


class Policy(Record):
    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("tenant_id"),)
    disabled_tools: Mapped[list] = mapped_column(JSON, default=list)
    stopped: Mapped[bool] = mapped_column(default=False)


class Document(Record):
    __tablename__ = "documents"
    __table_args__ = (tenant_reference("documents", "task_id", "tasks"),)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    project: Mapped[str] = mapped_column(String(80), index=True)
    source: Mapped[str] = mapped_column(String(300))
    version: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(String)
    allowed_subjects: Mapped[list] = mapped_column(JSON)
    revoked: Mapped[bool] = mapped_column(default=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RepairRun(Record):
    __tablename__ = "repair_runs"
    __table_args__ = (
        tenant_identity("repair_runs"),
        tenant_reference("repair_runs", "task_id", "tasks"),
        tenant_reference("repair_runs", "snapshot_id", "artifacts"),
        UniqueConstraint("tenant_id", "task_id"),
    )
    task_id: Mapped[str] = mapped_column(String(36))
    snapshot_id: Mapped[str] = mapped_column(String(36))
    source_digest: Mapped[str] = mapped_column(String(64))
    config_digest: Mapped[str] = mapped_column(String(64))
    max_attempts: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(40), default="ACTIVE")


class RepairAttempt(Record):
    __tablename__ = "repair_attempts"
    __table_args__ = (
        tenant_reference("repair_attempts", "run_id", "repair_runs"),
        tenant_reference("repair_attempts", "patch_artifact_id", "artifacts"),
        tenant_reference("repair_attempts", "verification_artifact_id", "artifacts"),
        UniqueConstraint("tenant_id", "run_id", "ordinal"),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    ordinal: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(40), default="GENERATING")
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    patch_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    verification_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    verification_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verification_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    verification_count: Mapped[int] = mapped_column(Integer, default=0)


class AdmissionGate(Record):
    __tablename__ = "admission_gates"
    __table_args__ = (UniqueConstraint("tenant_id"),)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    max_active: Mapped[int] = mapped_column(Integer, default=0)


class WorkLease(Record):
    __tablename__ = "work_leases"
    __table_args__ = (
        tenant_reference("work_leases", "task_id", "tasks"),
        UniqueConstraint("tenant_id", "task_id"),
    )
    task_id: Mapped[str] = mapped_column(String(36))
    owner_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DependencyCircuit(Record):
    __tablename__ = "dependency_circuits"
    __table_args__ = (UniqueConstraint("tenant_id", "dependency"),)
    dependency: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(40))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(20), default="CLOSED")
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    probe_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    max_in_flight: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class DependencyReadLease(Record):
    __tablename__ = "dependency_read_leases"
    __table_args__ = (
        Index("ix_dependency_read_leases_scope_expiry", "tenant_id", "dependency", "expires_at"),
        ForeignKeyConstraint(
            ["tenant_id", "dependency"],
            ["dependency_circuits.tenant_id", "dependency_circuits.dependency"],
            name="fk_dependency_read_leases_circuit_tenant",
        ),
    )
    dependency: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Database:
    def __init__(self, url: str):
        if url.startswith("sqlite:///", 0) and ":memory:" not in url:
            from pathlib import Path

            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, **options)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextlib.contextmanager
    def session(self, tenant: str):
        if not tenant:
            raise ValueError("Explicit tenant required")
        with self.sessions.begin() as s:
            if self.engine.dialect.name == "postgresql":
                s.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant}
                )
            yield s

    def create_schema(self):
        Base.metadata.create_all(self.engine)

    @contextlib.contextmanager
    def snapshot(self, tenant: str):
        """Read a consistent audit snapshot without blocking task mutations."""
        if not tenant:
            raise ValueError("Explicit tenant required")
        with self.sessions.begin() as s:
            if self.engine.dialect.name == "postgresql":
                s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                s.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant}
                )
            else:
                # sqlite3 legacy transaction mode does not BEGIN on SELECT by itself.
                s.connection().exec_driver_sql("BEGIN")
            yield s

    def assert_production_role(self):
        if self.engine.dialect.name != "postgresql":
            raise ValueError("Production requires PostgreSQL")
        with self.engine.connect() as c:
            unsafe = c.scalar(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
            owns_tables = c.scalar(
                text(
                    "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner=current_user"
                )
            )
            if unsafe or owns_tables:
                raise ValueError(
                    "Runtime database role must not own tables, bypass RLS, or be superuser"
                )


def tenant_get(s: Session, model, id: str, tenant: str, lock: bool = False):
    from sqlalchemy import select

    stmt = select(model).where(model.id == id, model.tenant_id == tenant)
    if lock:
        stmt = stmt.with_for_update()
    obj = s.scalar(stmt)
    if obj is None:
        from agent_py.domain import DomainError

        raise DomainError("NOT_FOUND", "Resource not found", 404)
    return obj


def emit(s: Session, task: Task, kind: str, payload: dict):
    from opentelemetry import trace
    from sqlalchemy import update

    span = trace.get_current_span().get_span_context()
    if span.is_valid:
        payload = {**payload, "trace_id": format(span.trace_id, "032x")}

    seq = s.scalar(
        update(Task)
        .where(Task.id == task.id, Task.tenant_id == task.tenant_id)
        .values(next_sequence=Task.next_sequence + 1)
        .returning(Task.next_sequence)
    )
    s.add(
        TaskEvent(
            tenant_id=task.tenant_id,
            task_id=task.id,
            sequence=seq,
            event_type=kind,
            payload=payload,
        )
    )
