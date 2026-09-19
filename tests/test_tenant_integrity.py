"""Database invariants tested through direct writes, bypassing Service authorization."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from agent_py.db import (
    Approval,
    Artifact,
    Base,
    Document,
    Operation,
    RepairAttempt,
    RepairRun,
    Reservation,
    Task,
    TaskEvent,
    WorkLease,
    now,
    uid,
)
from agent_py.domain import TaskContract

REFERENCES = [
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
]


def seed_graph(service, principal):
    tenant = principal.tenant_id
    task = service.create_task(
        principal,
        TaskContract(kind="repair", goal="Database constraint test", project="demo"),
        uid(),
    )
    artifact = Artifact(
        id=uid(),
        tenant_id=tenant,
        task_id=task.id,
        kind="fixture",
        digest="a" * 64,
        storage_key=uid(),
    )
    op = Operation(
        id=uid(),
        tenant_id=tenant,
        task_id=task.id,
        step_key="fixture",
        tool="read_ci",
        resource="demo",
        parameters={},
        payload_digest="a" * 64,
    )
    run = RepairRun(
        id=uid(),
        tenant_id=tenant,
        task_id=task.id,
        snapshot_id=artifact.id,
        source_digest="a" * 64,
        config_digest="b" * 64,
        max_attempts=1,
    )
    rows = [
        artifact,
        op,
        run,
        Approval(
            tenant_id=tenant,
            operation_id=op.id,
            payload_digest="a" * 64,
            expires_at=now() + timedelta(hours=1),
        ),
        RepairAttempt(
            tenant_id=tenant,
            run_id=run.id,
            ordinal=1,
            patch_artifact_id=artifact.id,
            verification_artifact_id=artifact.id,
        ),
        Document(
            tenant_id=tenant,
            task_id=task.id,
            project="demo",
            source="fixture",
            version="1",
            body="fixture",
            allowed_subjects=[],
        ),
        Reservation(tenant_id=tenant, task_id=task.id, call_key="fixture", maximum=0),
        WorkLease(tenant_id=tenant, task_id=task.id, expires_at=now()),
    ]
    with service.db.session(tenant) as s:
        s.add_all(rows)
    graph = {row.__tablename__: row.id for row in rows}
    graph["tasks"] = task.id
    with service.db.session(tenant) as s:
        for name in ("task_events", "outbox"):
            table = Base.metadata.tables[name]
            graph[name] = s.scalar(select(table.c.id).where(table.c.task_id == task.id))
    return graph


def reject_reference(db, tenant, graph, reference, target):
    table, column, _ = reference
    child = Base.metadata.tables[table]
    with pytest.raises(IntegrityError):
        with db.session(tenant) as s:
            s.execute(update(child).where(child.c.id == graph[table]).values({column: target}))
    with db.session(tenant) as s:
        assert s.scalar(select(child.c[column]).where(child.c.id == graph[table])) != target


@pytest.fixture
def graphs(env):
    service, p, _ = env
    return seed_graph(service, p), seed_graph(service, p.model_copy(update={"tenant_id": "t2"}))


@pytest.mark.parametrize("reference", REFERENCES, ids=lambda r: r[0] + "." + r[1])
@pytest.mark.parametrize("invalid", ["other_tenant", "missing"])
def test_reference_cannot_cross_tenant_or_dangle(env, graphs, reference, invalid):
    first, second = graphs
    reject_reference(
        env[0].db,
        "t1",
        first,
        reference,
        second[reference[2]] if invalid == "other_tenant" else uid(),
    )


def test_deferred_failure_rolls_back_whole_transaction(env, task):
    db = env[0].db
    with pytest.raises(IntegrityError):
        with db.session("t1") as s:
            s.execute(
                update(Task).where(Task.id == task.id).values(waiting_reason="must roll back")
            )
            s.add(
                TaskEvent(
                    tenant_id="t1", task_id=uid(), sequence=1, event_type="invalid", payload={}
                )
            )
            s.flush()  # The constraint is intentionally checked at commit, not flush.
    with db.session("t1") as s:
        assert s.get(Task, task.id).waiting_reason is None
        assert not s.scalar(select(TaskEvent).where(TaskEvent.event_type == "invalid"))


def test_nullable_project_document_and_non_cascading_deletion(env, graphs):
    service, _, _ = env
    first, _ = graphs
    with service.db.session("t1") as s:
        s.get(Document, first["documents"]).task_id = None
    for model, name in (
        (Task, "tasks"),
        (Operation, "operations"),
        (Artifact, "artifacts"),
        (RepairRun, "repair_runs"),
    ):
        with pytest.raises(IntegrityError):
            with service.db.session("t1") as s:
                s.delete(s.get(model, first[name]))
        with service.db.session("t1") as s:
            assert s.get(model, first[name]) is not None
