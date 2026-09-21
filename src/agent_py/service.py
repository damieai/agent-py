from datetime import UTC, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from agent_py.adapters.simulation import ConfirmedFailure
from agent_py.config import Settings
from agent_py.db import (
    Approval,
    Artifact,
    DailyBudget,
    Database,
    Operation,
    Outbox,
    Policy,
    Reservation,
    Task,
    emit,
    now,
    tenant_get,
    uid,
)
from agent_py.domain import (
    APPROVAL_TOOLS,
    ActionProposal,
    DomainError,
    Principal,
    TaskContract,
    digest,
)
from agent_py.observations import operation_attributes, stage, task_attributes
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
        if k in {"replicas", "expected_revision"} and (
            type(v) is not int or v < 1 or (k == "replicas" and v > 10)
        ):
            raise DomainError("INVALID_PARAMETERS", "Invalid integer range", 422)
        if k == "job" and v != "verify-candidate":
            raise DomainError("INVALID_PARAMETERS", "Job is not allowlisted", 422)


class Service:
    def __init__(self, db: Database, settings: Settings, remote):
        self.db, self.settings, self.remote = db, settings, remote
        from agent_py.releases import ReleaseGuard
        from agent_py.telemetry import Telemetry

        self.release = ReleaseGuard(settings)
        self.telemetry = Telemetry(settings)
        self.reconcile_cursors: dict[str, str] = {}

    def create_task(self, p: Principal, contract: TaskContract, key: str) -> Task:
        from agent_py.trace_context import enabled

        if not enabled(self, p.tenant_id):
            return self._create_task(p, contract, key)
        with self.telemetry.span(
            "task.accepted", new_root=True, **{"tenant.id": p.tenant_id}
        ) as span:
            task = self._create_task(p, contract, key)
            span.set_attribute("task.id", task.id)
            return task

    def _create_task(self, p: Principal, contract: TaskContract, key: str) -> Task:
        if contract.workflow == "investigation_loop" and self.settings.execution_mode != "live":
            raise DomainError("INVESTIGATION_MODE", "Investigation loop requires live mode", 409)
        if contract.workflow == "repair_candidate" and self.settings.execution_mode != "live":
            raise DomainError(
                "REPAIR_MODE", "Candidate repair requires live mode and explicit opt-ins", 409
            )
        if not key or len(key) > 160:
            raise DomainError("INVALID_KEY", "A bounded Idempotency-Key is required", 422)
        body = contract.model_dump()
        if body["workflow"] == "investigate":
            body.pop("workflow")  # Preserve request identity for pre-workflow contracts.
        try:
            with self.db.session(p.tenant_id) as s:
                existing = s.scalar(
                    select(Task).where(Task.tenant_id == p.tenant_id, Task.request_key == key)
                )
                if existing:
                    authorize(s, p, existing)
                    if existing.request_digest != digest(body):
                        raise DomainError(
                            "IDEMPOTENCY_CONFLICT", "Key already binds another request"
                        )
                    return existing
                self.release.check()
                if contract.release_id not in {"agent-v1", self.release.id}:
                    raise DomainError("RELEASE_MISMATCH", "Requested release is not served here")
                task = Task(
                    id=uid(),
                    tenant_id=p.tenant_id,
                    request_key=key,
                    request_digest=digest(body),
                    principal=p.subject,
                    contract={**body, "release_id": self.release.id},
                    deadline=now() + timedelta(seconds=contract.deadline_seconds),
                )
                authorize(s, p, task, "developer" if contract.kind == "repair" else "operator")
                from agent_py.scheduling import lock_tenant

                lock_tenant(s, p.tenant_id)
                raced = s.scalar(
                    select(Task).where(Task.tenant_id == p.tenant_id, Task.request_key == key)
                )
                if raced:
                    authorize(s, p, raced)
                    if raced.request_digest != digest(body):
                        raise DomainError("IDEMPOTENCY_CONFLICT", "Concurrent request differs")
                    return raced
                queued = s.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.tenant_id == p.tenant_id, Task.status == "QUEUED")
                )
                if queued >= self.settings.max_queue_per_tenant:
                    raise DomainError("QUEUE_FULL", "Tenant queue is full", 429)
                s.add(task)
                s.flush()
                s.add(Outbox(tenant_id=p.tenant_id, task_id=task.id))
                from agent_py.trace_context import record_origin

                record_origin(self, s, task)
                emit(s, task, "task.created", {"kind": contract.kind, "release": self.release.id})
                return task
        except IntegrityError:
            # Resolve concurrent duplicate creation through the same authorization path.
            with self.db.session(p.tenant_id) as s:
                existing = s.scalar(
                    select(Task).where(Task.tenant_id == p.tenant_id, Task.request_key == key)
                )
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
        name = "task.takeover" if takeover else "task.cancel"
        with stage(self.telemetry, name, p.tenant_id, task_id) as span:
            task = self._stop(p, task_id, takeover, span)
            span.set_attributes(task_attributes(task))

    def _stop(self, p, task_id, takeover, span):
        span.set_attribute("stage.changed", False)
        with self.db.session(p.tenant_id) as s:
            t = tenant_get(s, Task, task_id, p.tenant_id, True)
            authorize(s, p, t)
            if "operator" not in p.roles and (takeover or p.subject != t.principal):
                raise DomainError(
                    "FORBIDDEN",
                    "Only the task owner may cancel; takeover requires operator role",
                    403,
                )
            if t.status == "TERMINATED" or t.cancelled or (t.taken_over and takeover):
                return t
            changed = s.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.tenant_id == p.tenant_id,
                    Task.cancelled.is_(False),
                    Task.status != "TERMINATED",
                    Task.version == t.version,
                )
                .values(
                    cancelled=not takeover,
                    taken_over=takeover,
                    status="WAITING" if takeover else "CANCELLING",
                    waiting_reason="HUMAN_TAKEOVER" if takeover else "RECONCILIATION",
                    version=Task.version + 1,
                )
            )
            if changed.rowcount != 1:
                raise DomainError("TASK_CHANGED", "Task changed; refresh before retrying")
            emit(s, t, "task.takeover" if takeover else "task.cancelled", {})
            span.set_attribute("stage.changed", True)
            return t

    def resume(self, p: Principal, task_id: str, expected_version: int):
        with stage(self.telemetry, "task.resume", p.tenant_id, task_id) as span:
            task = self._resume(p, task_id, expected_version, span)
            span.set_attributes(task_attributes(task))
            return task

    def _resume(self, p, task_id, expected_version, span):
        span.set_attribute("stage.changed", False)
        with self.db.session(p.tenant_id) as s:
            t = tenant_get(s, Task, task_id, p.tenant_id, True)
            authorize(s, p, t, "operator")
            if t.version != expected_version:
                raise DomainError("TASK_CHANGED", "Task version changed; refresh before resuming")
            if t.cancelled or t.status == "TERMINATED":
                raise DomainError("TASK_STOPPED", "Cancelled or terminated tasks cannot resume")
            if not t.taken_over:
                return t
            if aware(t.deadline) <= now():
                raise DomainError("DEADLINE", "Resume cannot extend the original deadline")
            check_grant(
                s, t.tenant_id, t.principal, t.contract["project"], t.contract["environment"]
            )
            policy = s.scalar(select(Policy).where(Policy.tenant_id == p.tenant_id))
            if policy and policy.stopped:
                raise DomainError("EMERGENCY_STOP", "Tenant dispatch is disabled", 403)
            changed = s.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.tenant_id == p.tenant_id,
                    Task.version == expected_version,
                    Task.cancelled.is_(False),
                    Task.taken_over.is_(True),
                    Task.status != "TERMINATED",
                )
                .values(
                    taken_over=False, status="QUEUED", waiting_reason=None, version=Task.version + 1
                )
            )
            if changed.rowcount != 1:
                raise DomainError("TASK_CHANGED", "Task changed; refresh before resuming")
            emit(s, t, "task.resumed", {"actor": p.subject, "version": expected_version + 1})
            span.set_attribute("stage.changed", True)
            return t

    def propose(self, p: Principal, task_id: str, step: str, action: ActionProposal) -> Operation:
        with stage(self.telemetry, "operation.propose", p.tenant_id, task_id) as span:
            operation = self._propose(p, task_id, step, action, span)
            span.set_attributes(operation_attributes(self, operation))
            return operation

    def _propose(self, p, task_id, step, action, span):
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
            existing = s.scalar(
                select(Operation).where(
                    Operation.tenant_id == p.tenant_id,
                    Operation.task_id == t.id,
                    Operation.step_key == step,
                )
            )
            if existing:
                if existing.payload_digest != h:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Step already binds another action")
                span.set_attribute("stage.changed", False)
                return existing
            count = s.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.tenant_id == p.tenant_id, Operation.task_id == t.id)
            )
            if count >= 100:
                raise DomainError("TOOL_BUDGET", "Tool invocation limit reached", 429)
            op = Operation(
                id=uid(),
                tenant_id=p.tenant_id,
                task_id=t.id,
                step_key=step,
                tool=action.tool,
                resource=action.resource,
                parameters=action.parameters,
                payload_digest=h,
            )
            s.add(op)
            if action.tool in APPROVAL_TOOLS:
                s.add(
                    Approval(
                        tenant_id=p.tenant_id,
                        operation_id=op.id,
                        payload_digest=h,
                        expires_at=now() + timedelta(hours=24),
                    )
                )
                t.status, t.waiting_reason = "WAITING", "APPROVAL"
            emit(s, t, "operation.proposed", {"operation_id": op.id, "tool": op.tool})
            s.flush()
            span.set_attribute("stage.changed", True)
            return op

    def decide(self, p: Principal, approval_id: str, decision: str, expected_digest: str):
        # Task identity is attached only after the existing tenant/auth checks succeed.
        with self.telemetry.span("operation.approval", **{"tenant.id": p.tenant_id}) as span:
            try:
                approval = self._decide(p, approval_id, decision, expected_digest, span)
            except Exception:
                span.set_attribute("stage.outcome", "error")
                raise
            span.set_attribute("stage.outcome", "completed")
            span.set_attribute("stage.decision", approval.status)
            return approval

    def _decide(self, p, approval_id, decision, expected_digest, span):
        if decision not in {"approve", "reject"}:
            raise DomainError("INVALID_DECISION", "Invalid approval decision", 422)
        with self.db.session(p.tenant_id) as s:
            a = tenant_get(s, Approval, approval_id, p.tenant_id, True)
            op = tenant_get(s, Operation, a.operation_id, p.tenant_id)
            t = tenant_get(s, Task, op.task_id, p.tenant_id)
            authorize(s, p, t, "approver")
            span.set_attribute("task.id", t.id)
            span.set_attributes(operation_attributes(self, op))
            span.set_attribute("stage.changed", False)
            if t.contract["environment"] == "production" and p.subject == t.principal:
                raise DomainError(
                    "SEPARATION_OF_DUTIES", "Production approver must be independent", 403
                )
            if expected_digest != a.payload_digest or a.payload_digest != op.payload_digest:
                raise DomainError("APPROVAL_CHANGED", "Approval payload mismatch")
            target = "APPROVED" if decision == "approve" else "REJECTED"
            if a.status == target:
                return a
            if a.status != "PENDING" or aware(a.expires_at) <= now():
                raise DomainError("APPROVAL_CLOSED", "Approval expired or already decided")
            if t.cancelled or t.taken_over:
                raise DomainError("TASK_STOPPED", "Task no longer owns execution")
            changed = s.execute(
                update(Approval)
                .execution_options(synchronize_session="fetch")
                .where(
                    Approval.id == a.id,
                    Approval.tenant_id == p.tenant_id,
                    Approval.status == "PENDING",
                    Approval.expires_at > now(),
                )
                .values(status=target, decided_by=p.subject)
            )
            if changed.rowcount != 1:
                s.refresh(a)
                if a.status == target:
                    return a
                raise DomainError("APPROVAL_CLOSED", "Approval expired or already decided")
            emit(s, t, "approval.decided", {"operation_id": op.id, "decision": target})
            span.set_attribute("stage.changed", True)
            return a

    def _executable(self, s, t: Task):
        from agent_py.scheduling import check_execution_lease

        check_execution_lease(s, t)
        self.release.check(t.contract["release_id"])
        if t.cancelled or t.taken_over or t.status == "TERMINATED":
            raise DomainError("TASK_STOPPED", "Task cannot dispatch new actions")
        if aware(t.deadline) <= now():
            raise DomainError("DEADLINE", "Task deadline exceeded")
        check_grant(s, t.tenant_id, t.principal, t.contract["project"], t.contract["environment"])
        policy = s.scalar(select(Policy).where(Policy.tenant_id == t.tenant_id))
        if policy and policy.stopped:
            raise DomainError("EMERGENCY_STOP", "Tenant dispatch is disabled", 403)

    def execute(self, tenant: str, operation_id: str) -> Operation:
        # Commit dispatch intent before network I/O. Only the successful CAS can dispatch.
        with self.db.session(tenant) as s:
            op = tenant_get(s, Operation, operation_id, tenant, True)
            if op.status != "NOT_SUBMITTED":
                with stage(
                    self.telemetry,
                    "operation.result_reused",
                    tenant,
                    op.task_id,
                    **operation_attributes(self, op),
                ):
                    return op
            if op.tool not in self.remote.supported_tools:
                raise DomainError(
                    "CAPABILITY_UNAVAILABLE", "Write capability has not been certified", 503
                )
            t = tenant_get(s, Task, op.task_id, tenant, True)
            self._executable(s, t)
            policy = s.scalar(select(Policy).where(Policy.tenant_id == tenant))
            if policy and op.tool in policy.disabled_tools:
                raise DomainError("TOOL_DISABLED", "Tool is disabled", 403)
            if op.tool in APPROVAL_TOOLS:
                a = s.scalar(
                    select(Approval).where(
                        Approval.tenant_id == tenant, Approval.operation_id == op.id
                    )
                )
                if (
                    not a
                    or a.status != "APPROVED"
                    or aware(a.expires_at) <= now()
                    or a.payload_digest != op.payload_digest
                ):
                    raise DomainError("APPROVAL_REQUIRED", "Valid approval is required", 403)
                check_grant(
                    s, tenant, a.decided_by, t.contract["project"], t.contract["environment"]
                )
            claimed = s.execute(
                update(Operation)
                .where(
                    Operation.id == op.id,
                    Operation.tenant_id == tenant,
                    Operation.status == "NOT_SUBMITTED",
                )
                .values(status="PENDING", attempts=Operation.attempts + 1, updated_at=now())
            )
            if claimed.rowcount != 1:
                return op
            t.status, t.waiting_reason = "RUNNING", None
            emit(s, t, "operation.dispatched", {"operation_id": op.id})
            tool, resource, params = op.tool, op.resource, op.parameters
        with stage(
            self.telemetry,
            "operation.execute",
            tenant,
            op.task_id,
            **operation_attributes(self, op),
        ) as span:
            try:
                result = self.remote.execute(tenant, operation_id, tool, resource, params)
            except ConfirmedFailure as exc:
                recorded = self._record(tenant, operation_id, "FAILED", None, str(exc))
            except Exception as exc:
                # Even transport and local adapter errors are unknown after dispatch.
                recorded = self._record(tenant, operation_id, "UNKNOWN", None, type(exc).__name__)
            else:
                recorded = self._record(tenant, operation_id, "SUCCEEDED", result)
            span.set_attribute("stage.status", recorded.status)
            return recorded

    def _record(self, tenant: str, op_id: str, status: str, result: dict | None, error=None):
        if status == "SUCCEEDED" and (
            not isinstance(result, dict)
            or result.get("confirmed") is not True
            or not isinstance(result.get("external_id"), str)
            or not result["external_id"]
        ):
            status, result, error = "UNKNOWN", None, "UNVERIFIED_RECEIPT"
        with self.db.session(tenant) as s:
            op = tenant_get(s, Operation, op_id, tenant, True)
            if op.status == "SUCCEEDED":
                return op
            if op.status == "FAILED" and status != "SUCCEEDED":
                return op
            if op.status == "UNKNOWN" and status == "UNKNOWN":
                return op  # Preserve the first uncertainty timestamp and escalation state.
            values = dict(
                status=status,
                result=result,
                error=error,
                updated_at=op.updated_at if status == "UNKNOWN" else now(),
                recovery_status="RECONCILING" if status == "UNKNOWN" else None,
            )
            if result:
                values["external_id"] = result.get("external_id")
            condition = Operation.status != "SUCCEEDED"
            if status != "SUCCEEDED":
                condition = condition & (Operation.status != "FAILED")
            changed = s.execute(
                update(Operation)
                .where(Operation.id == op_id, Operation.tenant_id == tenant, condition)
                .values(**values)
            )
            if changed.rowcount != 1:
                s.refresh(op)
                return op
            t = tenant_get(s, Task, op.task_id, tenant, True)
            if status == "UNKNOWN":
                if not t.taken_over and not t.cancelled and t.status != "TERMINATED":
                    t.status, t.waiting_reason = "WAITING", "RECONCILIATION"
            emit(s, t, "operation." + status.lower(), {"operation_id": op.id, "error": error})
            return op

    def reconcile(self, tenant: str, operation_id: str):
        with self.db.session(tenant) as s:
            op = tenant_get(s, Operation, operation_id, tenant)
            if op.status not in {"UNKNOWN", "PENDING"}:
                return op
        with stage(
            self.telemetry,
            "operation.query",
            tenant,
            op.task_id,
            **operation_attributes(self, op),
        ) as span:
            result = self.remote.query(tenant, operation_id)
            if result is None:
                recorded = self._record(tenant, operation_id, "UNKNOWN", None, "NOT_YET_CONFIRMED")
            else:
                recorded = self._record(tenant, operation_id, "SUCCEEDED", result)
            span.set_attribute("stage.status", recorded.status)
            return recorded

    def escalate_uncertain(self, tenant: str, operation_id: str):
        result = self._escalate_uncertain(tenant, operation_id)
        if result is not None:
            operation, task = result
            # Emit only after the transaction commits; repeat scans do not invent transitions.
            with stage(
                self.telemetry,
                "operation.escalate",
                tenant,
                task.id,
                **operation_attributes(self, operation),
                **task_attributes(task),
                **{"stage.recovery_status": "MANUAL_REVIEW", "stage.changed": True},
            ):
                pass

    def _escalate_uncertain(self, tenant, operation_id):
        with self.db.session(tenant) as s:
            changed = s.execute(
                update(Operation)
                .where(
                    Operation.id == operation_id,
                    Operation.tenant_id == tenant,
                    Operation.status.in_(["PENDING", "UNKNOWN"]),
                    Operation.updated_at <= now() - timedelta(minutes=15),
                    (Operation.recovery_status.is_(None))
                    | (Operation.recovery_status != "MANUAL_REVIEW"),
                )
                .values(recovery_status="MANUAL_REVIEW")
                .execution_options(synchronize_session="fetch")
            )
            if changed.rowcount != 1:
                return
            op = tenant_get(s, Operation, operation_id, tenant)
            t = tenant_get(s, Task, op.task_id, tenant, True)
            if not t.cancelled and not t.taken_over and t.status != "TERMINATED":
                t.status, t.waiting_reason = "WAITING", "MANUAL_REVIEW"
            emit(
                s, t, "operation.escalated", {"operation_id": op.id, "reason": "UNCONFIRMED_15_MIN"}
            )
            return op, t

    def reserve(self, tenant: str, task_id: str, call_key: str, maximum: int):
        if maximum <= 0:
            raise DomainError("INVALID_BUDGET", "Reservation must be positive", 422)
        with self.db.session(tenant) as s:
            t = tenant_get(s, Task, task_id, tenant, True)
            self._executable(s, t)
            old = s.scalar(
                select(Reservation).where(
                    Reservation.tenant_id == tenant,
                    Reservation.task_id == task_id,
                    Reservation.call_key == call_key,
                )
            )
            if old:
                if old.maximum != maximum:
                    raise DomainError("BUDGET_CONFLICT", "Call reservation changed")
                return old
            day = now().date().isoformat()
            if s.bind.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            s.execute(
                insert(DailyBudget)
                .values(id=uid(), tenant_id=tenant, day=day, spent=0, reserved=0, created_at=now())
                .on_conflict_do_nothing(index_elements=["tenant_id", "day"])
            )
            daily = s.execute(
                update(DailyBudget)
                .where(
                    DailyBudget.tenant_id == tenant,
                    DailyBudget.day == day,
                    DailyBudget.spent + DailyBudget.reserved + maximum
                    <= self.settings.daily_budget_micro_usd,
                )
                .values(reserved=DailyBudget.reserved + maximum)
            )
            if daily.rowcount != 1:
                raise DomainError("DAILY_BUDGET_EXHAUSTED", "Daily budget exhausted", 429)
            updated = s.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.tenant_id == tenant,
                    Task.spent + Task.reserved + maximum <= t.contract["budget_micro_usd"],
                )
                .values(reserved=Task.reserved + maximum)
            )
            if updated.rowcount != 1:
                raise DomainError("BUDGET_EXHAUSTED", "Task budget exhausted", 429)
            r = Reservation(
                id=uid(),
                tenant_id=tenant,
                task_id=task_id,
                call_key=call_key,
                maximum=maximum,
                day=day,
            )
            s.add(r)
            return r

    def settle(self, tenant: str, reservation_id: str, actual: int, decision: dict | None = None):
        if actual < 0:
            raise DomainError("INVALID_COST", "Negative cost", 422)
        with self.db.session(tenant) as s:
            r = tenant_get(s, Reservation, reservation_id, tenant, True)
            if r.actual is not None:
                if r.actual != actual:
                    raise DomainError("SETTLEMENT_CONFLICT", "Already settled with another cost")
                return
            changed = s.execute(
                update(Reservation)
                .where(
                    Reservation.id == r.id,
                    Reservation.tenant_id == tenant,
                    Reservation.actual.is_(None),
                )
                .values(actual=actual, decision=decision)
            )
            if changed.rowcount != 1:
                s.refresh(r)
                if r.actual != actual:
                    raise DomainError("SETTLEMENT_CONFLICT", "Already settled with another cost")
                return
            s.execute(
                update(Task)
                .where(Task.id == r.task_id, Task.tenant_id == tenant)
                .values(reserved=Task.reserved - r.maximum, spent=Task.spent + actual)
            )
            s.execute(
                update(DailyBudget)
                .where(DailyBudget.tenant_id == tenant, DailyBudget.day == r.day)
                .values(reserved=DailyBudget.reserved - r.maximum, spent=DailyBudget.spent + actual)
            )

    def bind_inference(self, tenant: str, reservation_id: str, request_digest: str):
        with self.db.session(tenant) as s:
            s.execute(
                update(Reservation)
                .where(
                    Reservation.id == reservation_id,
                    Reservation.tenant_id == tenant,
                    Reservation.request_digest.is_(None),
                    Reservation.dispatched.is_(False),
                )
                .values(request_digest=request_digest)
            )
            r = tenant_get(s, Reservation, reservation_id, tenant)
            if r.request_digest != request_digest:
                raise DomainError("INFERENCE_CONFLICT", "Inference request changed")
            return r

    def claim_inference(self, tenant: str, reservation_id: str):
        with self.db.session(tenant) as s:
            r = tenant_get(s, Reservation, reservation_id, tenant, True)
            self._executable(s, tenant_get(s, Task, r.task_id, tenant, True))
            result = s.execute(
                update(Reservation)
                .where(
                    Reservation.id == r.id,
                    Reservation.tenant_id == tenant,
                    Reservation.dispatched.is_(False),
                    Reservation.actual.is_(None),
                )
                .values(dispatched=True)
            )
            if result.rowcount != 1:
                raise DomainError(
                    "INFERENCE_ALREADY_DISPATCHED",
                    "Read the saved result; do not repeat a possibly billed request",
                )

    def finish(self, tenant: str, task_id: str):
        with stage(self.telemetry, "task.finish", tenant, task_id) as span:
            task = self._finish(tenant, task_id, span)
            span.set_attributes(task_attributes(task))
            return task

    def _finish(self, tenant, task_id, span):
        span.set_attribute("stage.changed", False)
        with self.db.session(tenant) as s:
            t = tenant_get(s, Task, task_id, tenant, True)
            ops = s.scalars(
                select(Operation).where(Operation.tenant_id == tenant, Operation.task_id == task_id)
            ).all()
            if t.taken_over:
                return t
            if any(o.status in {"UNKNOWN", "PENDING"} for o in ops):
                span.set_attribute(
                    "stage.changed", (t.status, t.waiting_reason) != ("WAITING", "RECONCILIATION")
                )
                t.status, t.waiting_reason = "WAITING", "RECONCILIATION"
                return t
            if not t.cancelled and any(o.status == "NOT_SUBMITTED" for o in ops):
                return t
            result = (
                "CANCELLED"
                if t.cancelled
                else "FAILED"
                if any(o.status == "FAILED" for o in ops)
                else "SUCCESS"
            )
            if t.status == "TERMINATED":
                return t
            if result == "SUCCESS" and not s.scalar(
                select(Artifact.id).where(
                    Artifact.tenant_id == tenant,
                    Artifact.task_id == task_id,
                    Artifact.kind == "verification",
                )
            ):
                raise DomainError(
                    "VERIFICATION_REQUIRED", "Completion requires independent verification evidence"
                )
            t.status, t.result, t.waiting_reason = "TERMINATED", result, None
            emit(s, t, "task.terminated", {"result": result})
            span.set_attribute("stage.changed", True)
            return t
