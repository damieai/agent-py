"""Read-only operational views; user queries remain scoped to current grants."""

from datetime import UTC

from prometheus_client import CollectorRegistry, Gauge, generate_latest
from sqlalchemy import func, select

from agent_py.db import (
    AdmissionGate,
    DependencyCircuit,
    Grant,
    Operation,
    Outbox,
    RepairRun,
    Reservation,
    Task,
    WorkLease,
    now,
)
from agent_py.domain import DomainError


def summary(service, tenant, principal=None):
    when = now()
    scope = [Task.tenant_id == tenant]
    if principal is not None:
        if principal.tenant_id != tenant or "operator" not in principal.roles:
            raise DomainError("FORBIDDEN", "Operations view requires operator role", 403)
        scope.extend(
            [
                Task.contract["project"].as_string().in_(principal.projects),
                Task.contract["environment"].as_string().in_(principal.environments),
                select(Grant.id)
                .where(
                    Grant.tenant_id == tenant,
                    Grant.subject == principal.subject,
                    Grant.project == Task.contract["project"].as_string(),
                    Grant.environment == Task.contract["environment"].as_string(),
                    Grant.revoked.is_(False),
                )
                .exists(),
            ]
        )
    with service.db.session(tenant) as s:
        configured_limit = s.scalar(
            select(AdmissionGate.max_active).where(AdmissionGate.tenant_id == tenant)
        )
        task_ids = select(Task.id).where(*scope)
        states = dict(
            s.execute(select(Task.status, func.count()).where(*scope).group_by(Task.status)).all()
        )
        operation_states = dict(
            s.execute(
                select(Operation.status, func.count())
                .where(Operation.tenant_id == tenant, Operation.task_id.in_(task_ids))
                .group_by(Operation.status)
            ).all()
        )
        oldest = s.scalar(
            select(func.min(Operation.updated_at)).where(
                Operation.tenant_id == tenant,
                Operation.task_id.in_(task_ids),
                Operation.status.in_(["PENDING", "UNKNOWN"]),
            )
        )
        if oldest and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        waiting_oldest = s.scalar(
            select(func.min(WorkLease.enqueued_at)).where(
                WorkLease.tenant_id == tenant,
                WorkLease.task_id.in_(task_ids),
                WorkLease.owner_token.is_(None),
                WorkLease.expires_at > when,
            )
        )
        if waiting_oldest and waiting_oldest.tzinfo is None:
            waiting_oldest = waiting_oldest.replace(tzinfo=UTC)
        leases = dict(
            s.execute(
                select(WorkLease.owner_token.is_not(None), func.count())
                .where(
                    WorkLease.tenant_id == tenant,
                    WorkLease.task_id.in_(task_ids),
                    WorkLease.expires_at > when,
                )
                .group_by(WorkLease.owner_token.is_not(None))
            ).all()
        )
        repairs = dict(
            s.execute(
                select(RepairRun.state, func.count())
                .where(RepairRun.tenant_id == tenant, RepairRun.task_id.in_(task_ids))
                .group_by(RepairRun.state)
            ).all()
        )
        spend = s.execute(
            select(
                func.coalesce(func.sum(Reservation.actual), 0),
                func.coalesce(
                    func.sum(Reservation.maximum).filter(Reservation.actual.is_(None)), 0
                ),
            ).where(
                Reservation.tenant_id == tenant,
                Reservation.task_id.in_(task_ids),
                Reservation.day == when.date().isoformat(),
            )
        ).one()
        pending_outbox = s.scalar(
            select(func.count())
            .select_from(Outbox)
            .where(
                Outbox.tenant_id == tenant,
                Outbox.task_id.in_(task_ids),
                Outbox.delivered.is_(False),
            )
        )
        overdue = s.scalar(
            select(func.count())
            .select_from(Task)
            .where(*scope, Task.status != "TERMINATED", Task.deadline < when)
        )
    return {
        "tenant": tenant,
        "observed_at": when.isoformat(),
        "tasks": states,
        "operations": operation_states,
        "repairs": repairs,
        "admission": {
            "active": leases.get(True, 0),
            "waiting": leases.get(False, 0),
            "limit": configured_limit or service.settings.max_active_per_tenant,
            "oldest_wait_seconds": max(0, (when - waiting_oldest).total_seconds())
            if waiting_oldest
            else 0,
        },
        "oldest_unconfirmed_seconds": max(0, (when - oldest).total_seconds()) if oldest else 0,
        "pending_outbox": pending_outbox,
        "overdue_tasks": overdue,
        "today_spent_micro_usd": spend[0],
        "today_reserved_micro_usd": spend[1],
    }


def prometheus_snapshot(service):
    registry = CollectorRegistry()
    tasks = Gauge("agent_tasks", "Durable task state", ["tenant", "state"], registry=registry)
    ops = Gauge(
        "agent_operations", "Durable operation state", ["tenant", "state"], registry=registry
    )
    leases = Gauge(
        "agent_admission", "Current worker admission", ["tenant", "state"], registry=registry
    )
    age = Gauge(
        "agent_oldest_unconfirmed_seconds",
        "Age of oldest unconfirmed action",
        ["tenant"],
        registry=registry,
    )
    backlog = Gauge(
        "agent_outbox_pending", "Undelivered workflow starts", ["tenant"], registry=registry
    )
    spent = Gauge(
        "agent_today_spent_micro_usd", "Settled model cost today", ["tenant"], registry=registry
    )
    reserved = Gauge(
        "agent_today_reserved_micro_usd",
        "Unsettled model reservations today",
        ["tenant"],
        registry=registry,
    )
    circuits = Gauge(
        "agent_dependency_circuits",
        "Shared enterprise read circuit counts",
        ["tenant", "provider", "state"],
        registry=registry,
    )
    read_slots = Gauge(
        "agent_dependency_read_slots",
        "Shared enterprise read slots summed by provider",
        ["tenant", "provider", "state"],
        registry=registry,
    )
    for tenant in dict.fromkeys(service.settings.monitoring_tenants):
        from agent_py.resilience import dependency_status

        dependencies = dependency_status(service, tenant)
        with service.db.session(tenant) as s:
            counts = {
                (provider, state): count
                for provider, state, count in s.execute(
                    select(DependencyCircuit.provider, DependencyCircuit.state, func.count())
                    .where(DependencyCircuit.tenant_id == tenant)
                    .group_by(DependencyCircuit.provider, DependencyCircuit.state)
                )
            }
        for provider in ("bitbucket_pr", "jira_issue", "jenkins_build", "kubernetes_deployment"):
            read_slots.labels(tenant, provider, "active").set(
                sum(row["active_reads"] for row in dependencies if row["provider"] == provider)
            )
            read_slots.labels(tenant, provider, "limit").set(
                sum(row["read_limit"] for row in dependencies if row["provider"] == provider)
            )
            for state in ("CLOSED", "OPEN", "HALF_OPEN"):
                circuits.labels(tenant, provider, state).set(counts.get((provider, state), 0))
        data = summary(service, tenant)
        for state in ("QUEUED", "RUNNING", "WAITING", "CANCELLING", "TERMINATED"):
            tasks.labels(tenant, state).set(data["tasks"].get(state, 0))
        for state in ("NOT_SUBMITTED", "PENDING", "UNKNOWN", "SUCCEEDED", "FAILED"):
            ops.labels(tenant, state).set(data["operations"].get(state, 0))
        for state in ("active", "waiting", "limit"):
            leases.labels(tenant, state).set(data["admission"][state])
        age.labels(tenant).set(data["oldest_unconfirmed_seconds"])
        backlog.labels(tenant).set(data["pending_outbox"])
        spent.labels(tenant).set(data["today_spent_micro_usd"])
        reserved.labels(tenant).set(data["today_reserved_micro_usd"])
    return generate_latest(service.telemetry.registry) + generate_latest(registry)
