"""Explicit metadata-only stage boundaries; never capture arguments or return values."""

from contextlib import contextmanager

from opentelemetry.trace import INVALID_SPAN

WAIT_REASONS = {
    "HUMAN_REVIEW",
    "HUMAN_TAKEOVER",
    "EVIDENCE_REQUIRED",
    "APPROVAL",
    "RECONCILIATION",
    "MANUAL_REVIEW",
    "INVESTIGATION_CONTINUE",
    "TASK_STOPPED",
    "CANDIDATE_READY_FOR_REVIEW",
    "REPAIR_EXHAUSTED",
    "REPAIR_INCONCLUSIVE",
    "REPAIR_INPUT_CHANGED",
    "REPAIR_EVIDENCE_CHANGED",
    "VERIFICATION_PENDING",
    "VERIFICATION_INTERRUPTED",
    "VERIFICATION_SUPERSEDED",
    "WORKER_LEASE_LOST",
    "BUDGET_EXHAUSTED",
    "DAILY_BUDGET_EXHAUSTED",
    "DEADLINE",
    "EMERGENCY_STOP",
    "FORBIDDEN",
    "PERMISSION_REVOKED",
    "MODEL_CALL_ALREADY_SETTLED",
    "MODEL_API_DISABLED",
    "REPAIR_DISABLED",
    "OTHER",
}
WORKER_PHASES = {"SNAPSHOT_SAVED", "ATTEMPT_CREATED", "PATCH_GENERATED", "CANDIDATE_FAILED"}


def worker_result_attributes(result):
    values = {}
    reason = result.get("wait")
    if isinstance(reason, str) and reason:
        values["stage.waiting_reason"] = reason if reason in WAIT_REASONS else "OTHER"
    phase = result.get("phase")
    if isinstance(phase, str) and phase in WORKER_PHASES:
        values["stage.phase"] = phase
    values["stage.blocked"] = bool(result.get("blocked"))
    if isinstance(result.get("result"), str) and result["result"] in {
        "SUCCESS",
        "FAILED",
        "CANCELLED",
    }:
        values["stage.task_result"] = result["result"]
    return values


def repair_lineage(run_id=None, ordinal=None, verification_id=None):
    """Observation-only identity; never add these fields to a paid model request."""
    values = {}
    if isinstance(run_id, str) and 0 < len(run_id) <= 160:
        values["repair.run_id"] = run_id
        if type(ordinal) is int and 1 <= ordinal <= 3:
            values["repair.candidate_ordinal"] = ordinal
    if isinstance(verification_id, str) and 0 < len(verification_id) <= 160:
        values["verification.id"] = verification_id
    return values


@contextmanager
def stage(telemetry, name, tenant, task_id, **attributes):
    if telemetry is None or task_id is None:
        yield INVALID_SPAN
        return
    with telemetry.span(name, **{"tenant.id": tenant, "task.id": task_id, **attributes}) as span:
        try:
            yield span
        except Exception:
            span.set_attribute("stage.outcome", "error")
            raise
        else:
            span.set_attribute("stage.outcome", "completed")


def operation_attributes(service, operation):
    return {
        "operation.id": operation.id,
        "stage.tool": operation.tool,
        "stage.attempt": operation.attempts,
        "stage.status": operation.status,
        "stage.execution_mode": service.settings.execution_mode,
        "stage.action_kind": "rollback" if operation.tool == "rollback" else "standard",
    }


def task_attributes(task):
    values = {
        "stage.task_status": task.status,
        "stage.cancelled": task.cancelled,
        "stage.taken_over": task.taken_over,
        "stage.version": task.version,
    }
    if task.result is not None:
        values["stage.task_result"] = task.result
    if task.waiting_reason is not None:
        values["stage.waiting_reason"] = task.waiting_reason
    return values
