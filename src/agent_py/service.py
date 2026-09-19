from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from agent_py.adapters.simulation import ConfirmedFailure, UnknownOutcome
from agent_py.config import Settings
from agent_py.db import (
    Approval, Database, Operation, Outbox, Reservation, Task, emit, now, tenant_get, uid,
)
from agent_py.domain import APPROVAL_TOOLS, ActionProposal, DomainError, Principal, TaskContract, digest
from agent_py.security import authorize, check_grant


def aware(dt):
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def validate_parameters(tool: str, p: dict):
    keys = {
        "create_pr": {"candidate_sha", "base_sha"},
        "trigger_ci": {"candidate_sha", "job"},
        "merge_pr": {"source_sha", "target_sha"},
        "deploy": {"image_digest", "expected_revision"},
        "rollback": {"image_digest", "expected_revision"},
        "runbook": {"replicas", "expected_revision"},
    }
    if set(p) != keys[tool]:
        raise DomainError("INVALID_PARAMETERS", "Unexpected or missing tool parameters", 422)
    for k, v in p.items():
        if k.endswith("sha"):
            import re
            if not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", v):
                raise DomainError("INVALID_PARAMETERS", "Expected immutable commit SHA", 422)
        if k == "image_digest":
            import re
            if not isinstance(v, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", v):
                raise DomainError("INVALID_PARAMETERS", "Expected immutable image digest", 422)
        if k in {"replicas", "expected_revision"} and (type(v) is not int or v < 1 or (k == "replicas" and v > 10)):
            raise DomainError("INVALID_PARAMETERS", "Invalid integer range", 422)
        if k == "job" and v != "verify-candidate":
            raise DomainError("INVALID_PARAMETERS", "Job is not allowlisted", 422)


class Service:
    def __init__(self, db: Database, settings: Settings, remote):
        self.db, self.settings, self.remote = db, settings, remote

    def create_task(self, p: Principal, contract: TaskContract, key: str) -> Task:
        if not key or len(key) > 160:
            raise DomainError("INVALID_KEY", "A bounded Idempotency-Key is required", 422)
        body = contract.model_dump()
        try:
            with self.db.session(p.tenant_id) as s:
                existing = s.scalar(select(Task).where(Task.tenant_id == p.tenant_id,
                                                       Task.request_key == key))
                if existing:
                    authorize(s, p, existing)
                    if existing.request_digest != digest(body):
                        raise DomainError("IDEMPOTENCY_CONFLICT", "Key already binds another request")
                    return existing
                task = Task(id=uid(), tenant_id=p.tenant_id, request_key=key,
                            request_digest=digest(body), principal=p.subject, contract=body,
                            deadline=now() + timedelta(seconds=contract.deadline_seconds))
                authorize(s, p, task, "developer" if contract.kind == "repair" else "operator")
                queued = s.scalar(select(func.count()).select_from(Task).where(
                    Task.tenant_id == p.tenant_id, Task.status == "QUEUED"))
                if queued >= self.settings.max_queue_per_tenant:
                    raise DomainError("QUEUE_FULL", "Tenant queue is full", 429)
                s.add(task)
                s.flush()
                s.add(Outbox(tenant_id=p.tenant_id, task_id=task.id))
                emit(s, task, "task.created", {"kind": contract.kind, "release": contract.release_id})
                return task
        except IntegrityError:
            # Resolve concurrent duplicate creation through the same authorization path.
            with self.db.session(p.tenant_id) as s:
                existing = s.scalar(select(Task).where(Task.tenant_id == p.tenant_id, Task.request_key == key))
                if not existing:
                    raise
                authorize(s, p, existing)
                if existing.request_digest != digest(body):
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Concurrent request differs")
                return existing

    def get_task(self, p: Principal, task_id: str) -> Task:
        with self.db.session(p.tenant_id) as s:
            t = tenant_get(s, Task, task_id, p.tenant_id)
            authorize(s, p, t)
            return t

    def stop(self, p: Principal, task_id: str, takeover: bool = False):
        with self.db.session(p.tenant_id) as s:
            t = tenant_get(s, Task, task_id, p.tenant_id, True)
            authorize(s, p, t, "operator")
            t.cancelled = not takeover
            t.taken_over = takeover
            t.status = "WAITING" if takeover else "CANCELLING"
            t.waiting_reason = "HUMAN_TAKEOVER" if takeover else "RECONCILIATION"
            emit(s, t, "task.takeover" if takeover else "task.cancelled", {})

    def propose(self, p: Principal, task_id: str, step: str, action: ActionProposal) -> Operation:
        validate_parameters(action.tool, action.parameters)
        if not step or len(step) > 160:
            raise DomainError("INVALID_STEP", "A bounded step identity is required", 422)
        with self.db.session(p.tenant_id) as s:
            t = tenant_get(s, Task, task_id, p.tenant_id, True)
            authorize(s, p, t, "developer" if t.contract["kind"] == "repair" else "operator")
            self._executable(s, t)
            if action.resource != t.contract["resource"]:
                raise DomainError("RESOURCE_SCOPE", "Action is outside task resource", 403)
            h = digest(action.model_dump())
            existing = s.scalar(select(Operation).where(Operation.tenant_id == p.tenant_id,
                               Operation.task_id == t.id, Operation.step_key == step))
            if existing:
                if existing.payload_digest != h:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Step already binds another action")
                return existing
            count = s.scalar(select(func.count()).select_from(Operation).where(
                Operation.tenant_id == p.tenant_id, Operation.task_id == t.id))
            if count >= 100:
                raise DomainError("TOOL_BUDGET", "Tool invocation limit reached", 429)
            op = Operation(id=uid(), tenant_id=p.tenant_id, task_id=t.id, step_key=step,
                           tool=action.tool, resource=action.resource, parameters=action.parameters,
                           payload_digest=h)
            s.add(op)
            if action.tool in APPROVAL_TOOLS:
                s.add(Approval(tenant_id=p.tenant_id, operation_id=op.id, payload_digest=h,
                              expires_at=now() + timedelta(hours=24)))
                t.status, t.waiting_reason = "WAITING", "APPROVAL"
            emit(s, t, "operation.proposed", {"operation_id": op.id, "tool": op.tool})
            s.flush()
            return op

    def decide(self, p: Principal, approval_id: str, decision: str, expected_digest: str):
        if decision not in {"approve", "reject"}:
            raise DomainError("INVALID_DECISION", "Invalid approval decision", 422)
        with self.db.session(p.tenant_id) as s:
            a = tenant_get(s, Approval, approval_id, p.tenant_id, True)
            op = tenant_get(s, Operation, a.operation_id, p.tenant_id)
            t = tenant_get(s, Task, op.task_id, p.tenant_id)
            authorize(s, p, t, "approver")
            if t.contract["environment"] == "production" and p.subject == t.principal:
                raise DomainError("SEPARATION_OF_DUTIES", "Production approver must be independent", 403)
            if expected_digest != a.payload_digest or a.payload_digest != op.payload_digest:
                raise DomainError("APPROVAL_CHANGED", "Approval payload mismatch")
            target = "APPROVED" if decision == "approve" else "REJECTED"
            if a.status == target:
                return a
            if a.status != "PENDING" or aware(a.expires_at) <= now():
                raise DomainError("APPROVAL_CLOSED", "Approval expired or already decided")
            if t.cancelled or t.taken_over:
                raise DomainError("TASK_STOPPED", "Task no longer owns execution")
            a.status, a.decided_by = target, p.subject
            emit(s, t, "approval.decided", {"operation_id": op.id, "decision": target})
            return a

    def _executable(self, s, t: Task):
        if t.cancelled or t.taken_over or t.status == "TERMINATED":
            raise DomainError("TASK_STOPPED", "Task cannot dispatch new actions")
        if aware(t.deadline) <= now():
            raise DomainError("DEADLINE", "Task deadline exceeded")
        check_grant(s, t.tenant_id, t.principal, t.contract["project"], t.contract["environment"])

    def execute(self, tenant: str, operation_id: str) -> Operation:
        # Commit dispatch intent before network I/O. Only the successful CAS can dispatch.
        with self.db.session(tenant) as s:
            op = tenant_get(s, Operation, operation_id, tenant, True)
            if op.status != "NOT_SUBMITTED":
                return op
            t = tenant_get(s, Task, op.task_id, tenant, True)
            self._executable(s, t)
            if op.tool in APPROVAL_TOOLS:
                a = s.scalar(select(Approval).where(Approval.tenant_id == tenant,
                                                    Approval.operation_id == op.id))
                if not a or a.status != "APPROVED" or aware(a.expires_at) <= now() or a.payload_digest != op.payload_digest:
                    raise DomainError("APPROVAL_REQUIRED", "Valid approval is required", 403)
                check_grant(s, tenant, a.decided_by, t.contract["project"], t.contract["environment"])
            claimed = s.execute(update(Operation).where(Operation.id == op.id,
                       Operation.tenant_id == tenant, Operation.status == "NOT_SUBMITTED")
                       .values(status="PENDING", attempts=Operation.attempts + 1, updated_at=now()))
            if claimed.rowcount != 1:
                return op
            t.status, t.waiting_reason = "RUNNING", None
            emit(s, t, "operation.dispatched", {"operation_id": op.id})
            tool, resource, params = op.tool, op.resource, op.parameters
        try:
            result = self.remote.execute(tenant, operation_id, tool, resource, params)
        except ConfirmedFailure as exc:
            return self._record(tenant, operation_id, "FAILED", None, str(exc))
        except Exception as exc:
            # Even transport and local adapter errors are conservatively unknown after dispatch.
            return self._record(tenant, operation_id, "UNKNOWN", None, type(exc).__name__)
        return self._record(tenant, operation_id, "SUCCEEDED", result)

    def _record(self, tenant: str, op_id: str, status: str, result: dict | None, error=None):
        with self.db.session(tenant) as s:
            op = tenant_get(s, Operation, op_id, tenant, True)
            if op.status == "SUCCEEDED":
                return op
            if op.status == "FAILED" and status != "SUCCEEDED":
                return op
            op.status, op.result, op.error, op.updated_at = status, result, error, now()
            op.recovery_status = "RECONCILING" if status == "UNKNOWN" else None
            if result:
                op.external_id = result.get("external_id")
            t = tenant_get(s, Task, op.task_id, tenant, True)
            if status == "UNKNOWN":
                t.status, t.waiting_reason = "WAITING", "RECONCILIATION"
            emit(s, t, "operation." + status.lower(), {"operation_id": op.id, "error": error})
            return op

    def reconcile(self, tenant: str, operation_id: str):
        with self.db.session(tenant) as s:
            op = tenant_get(s, Operation, operation_id, tenant)
            if op.status not in {"UNKNOWN", "PENDING"}:
                return op
        result = self.remote.query(tenant, operation_id)
        if result is None:
            return self._record(tenant, operation_id, "UNKNOWN", None, "NOT_YET_CONFIRMED")
        return self._record(tenant, operation_id, "SUCCEEDED", result)

    def reserve(self, tenant: str, task_id: str, call_key: str, maximum: int):
        if maximum <= 0:
            raise DomainError("INVALID_BUDGET", "Reservation must be positive", 422)
        with self.db.session(tenant) as s:
            t = tenant_get(s, Task, task_id, tenant, True)
            self._executable(s, t)
            old = s.scalar(select(Reservation).where(Reservation.tenant_id == tenant,
                           Reservation.task_id == task_id, Reservation.call_key == call_key))
            if old:
                if old.maximum != maximum:
                    raise DomainError("BUDGET_CONFLICT", "Call reservation changed")
                return old
            updated = s.execute(update(Task).where(Task.id == task_id, Task.tenant_id == tenant,
                         Task.spent + Task.reserved + maximum <= t.contract["budget_micro_usd"])
                         .values(reserved=Task.reserved + maximum))
            if updated.rowcount != 1:
                raise DomainError("BUDGET_EXHAUSTED", "Task budget exhausted", 429)
            r = Reservation(id=uid(), tenant_id=tenant, task_id=task_id,
                            call_key=call_key, maximum=maximum)
            s.add(r)
            return r

    def settle(self, tenant: str, reservation_id: str, actual: int):
        if actual < 0:
            raise DomainError("INVALID_COST", "Negative cost", 422)
        with self.db.session(tenant) as s:
            r = tenant_get(s, Reservation, reservation_id, tenant, True)
            if r.actual is not None:
                if r.actual != actual:
                    raise DomainError("SETTLEMENT_CONFLICT", "Already settled with another cost")
                return
            r.actual = actual
            s.execute(update(Task).where(Task.id == r.task_id, Task.tenant_id == tenant)
                      .values(reserved=Task.reserved - r.maximum, spent=Task.spent + actual))

    def finish(self, tenant: str, task_id: str):
        with self.db.session(tenant) as s:
            t = tenant_get(s, Task, task_id, tenant, True)
            ops = s.scalars(select(Operation).where(Operation.tenant_id == tenant,
                                                    Operation.task_id == task_id)).all()
            if t.taken_over:
                return t
            if any(o.status in {"UNKNOWN", "PENDING"} for o in ops):
                t.status, t.waiting_reason = "WAITING", "RECONCILIATION"
                return t
            if not t.cancelled and any(o.status == "NOT_SUBMITTED" for o in ops):
                return t
            result = "CANCELLED" if t.cancelled else "FAILED" if any(o.status == "FAILED" for o in ops) else "SUCCESS"
            if t.status == "TERMINATED":
                return t
            t.status, t.result, t.waiting_reason = "TERMINATED", result, None
            emit(s, t, "task.terminated", {"result": result})
            return t
