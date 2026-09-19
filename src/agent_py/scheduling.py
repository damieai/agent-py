"""Cross-worker tenant admission. External systems still need their own fencing contracts."""

from contextvars import ContextVar
from datetime import timedelta

from sqlalchemy import delete, func, select, update

from agent_py.db import AdmissionGate, Task, WorkLease, now, tenant_get, uid
from agent_py.domain import DomainError

execution_lease = ContextVar("execution_lease", default=None)


def lock_tenant(s, tenant):
    if s.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    s.execute(
        insert(AdmissionGate)
        .values(id=uid(), tenant_id=tenant, revision=0, created_at=now())
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    s.execute(
        update(AdmissionGate)
        .where(AdmissionGate.tenant_id == tenant)
        .values(revision=AdmissionGate.revision + 1)
    )


def acquire(service, tenant, task_id):
    with service.db.session(tenant) as s:
        lock_tenant(s, tenant)
        task = tenant_get(s, Task, task_id, tenant)
        if task.status == "TERMINATED" or task.cancelled or task.taken_over:
            return None, "CONTROL_ONLY"
        when = now()
        s.execute(
            delete(WorkLease).where(WorkLease.tenant_id == tenant, WorkLease.expires_at <= when)
        )
        stopped = select(Task.id).where(
            Task.tenant_id == tenant,
            (Task.status == "TERMINATED") | Task.cancelled.is_(True) | Task.taken_over.is_(True),
        )
        s.execute(
            delete(WorkLease).where(
                WorkLease.tenant_id == tenant,
                WorkLease.owner_token.is_(None),
                WorkLease.task_id.in_(stopped),
            )
        )
        ticket = s.scalar(
            select(WorkLease).where(WorkLease.tenant_id == tenant, WorkLease.task_id == task_id)
        )
        if ticket and ticket.owner_token:
            return None, "DUPLICATE_TICK"
        if not ticket:
            ticket = WorkLease(
                id=uid(),
                tenant_id=tenant,
                task_id=task_id,
                enqueued_at=when,
                expires_at=when + timedelta(seconds=90),
            )
            s.add(ticket)
        else:
            ticket.expires_at = when + timedelta(seconds=90)
        s.flush()
        active = s.scalar(
            select(func.count())
            .select_from(WorkLease)
            .where(
                WorkLease.tenant_id == tenant,
                WorkLease.owner_token.is_not(None),
                WorkLease.expires_at > when,
            )
        )
        gate = s.scalar(select(AdmissionGate).where(AdmissionGate.tenant_id == tenant))
        if gate.max_active == 0:
            gate.max_active = service.settings.max_active_per_tenant
        available = gate.max_active - active
        if available <= 0:
            return None, "TENANT_CAPACITY"
        first = s.scalars(
            select(WorkLease.id)
            .where(WorkLease.tenant_id == tenant, WorkLease.owner_token.is_(None))
            .order_by(WorkLease.enqueued_at, WorkLease.id)
            .limit(available)
        ).all()
        if ticket.id not in first:
            return None, "TENANT_QUEUE"
        token = uid()
        ticket.owner_token, ticket.expires_at = (
            token,
            when + timedelta(seconds=service.settings.worker_lease_seconds),
        )
        return token, None


def release(service, tenant, task_id, token):
    with service.db.session(tenant) as s:
        s.execute(
            delete(WorkLease).where(
                WorkLease.tenant_id == tenant,
                WorkLease.task_id == task_id,
                WorkLease.owner_token == token,
            )
        )


def check_execution_lease(s, task):
    lease = execution_lease.get()
    if lease is None:
        return  # Explicit operator CLI/library execution has no Worker admission context.
    tenant, task_id, token = lease
    if (
        tenant != task.tenant_id
        or task_id != task.id
        or not s.scalar(
            select(WorkLease.id).where(
                WorkLease.tenant_id == tenant,
                WorkLease.task_id == task_id,
                WorkLease.owner_token == token,
                WorkLease.expires_at > now(),
            )
        )
    ):
        raise DomainError("WORKER_LEASE_LOST", "Worker admission lease expired or was superseded")
