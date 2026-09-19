"""Versioned audit snapshots: integrity and internal consistency, not remote attestation."""

import json
from typing import Literal

from pydantic import Field, ValidationError
from sqlalchemy import select

from agent_py.db import Approval, Artifact, Operation, Task, TaskEvent, tenant_get
from agent_py.domain import APPROVAL_TOOLS, Contract, DomainError, TaskContract, digest
from agent_py.security import authorize

MAX_RECORDING_BYTES = 10_000_000


class AuditTask(Contract):
    id: str
    tenant: str
    contract: TaskContract
    version: int = Field(ge=1)
    status: Literal["QUEUED", "RUNNING", "WAITING", "CANCELLING", "TERMINATED"]
    result: Literal["SUCCESS", "FAILED", "CANCELLED"] | None
    sequence: int = Field(ge=0, le=10000)


class AuditOperation(Contract):
    id: str
    request: dict
    request_digest: str
    payload_digest: str
    status: Literal["NOT_SUBMITTED", "PENDING", "UNKNOWN", "SUCCEEDED", "FAILED"]
    attempts: int = Field(ge=0, le=1)
    result: dict | None


class AuditEvent(Contract):
    sequence: int = Field(ge=1)
    type: str = Field(min_length=1, max_length=60)
    payload: dict


class AuditApproval(Contract):
    id: str
    operation_id: str
    payload_digest: str
    status: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED"]


class AuditArtifact(Contract):
    id: str
    kind: str
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class AuditBody(Contract):
    schema_version: Literal["recording-v2"] = Field(alias="schema")
    release: Literal["agent-v1"]
    mode: Literal["simulation", "live"]
    task: AuditTask
    operations: list[AuditOperation] = Field(max_length=100)
    approvals: list[AuditApproval] = Field(max_length=100)
    events: list[AuditEvent] = Field(max_length=10000)
    artifacts: list[AuditArtifact] = Field(max_length=1000)


def check_recording(recording: dict) -> dict:
    def reject(message):
        raise DomainError("RECORDING_INVALID", message, 422)

    try:
        if len(json.dumps(recording, ensure_ascii=False).encode()) > MAX_RECORDING_BYTES:
            reject("Recording exceeds 10 MB")
        if set(recording) != {"body", "digest"} or recording["digest"] != digest(recording["body"]):
            reject("Recording checksum mismatch")
        body = AuditBody.model_validate(recording["body"])
    except (ValidationError, TypeError, ValueError, KeyError, RecursionError) as exc:
        raise DomainError("RECORDING_INVALID", "Malformed audit recording", 422) from exc
    ops = {o.id: o for o in body.operations}
    approvals = {a.operation_id: a for a in body.approvals}
    if len(ops) != len(body.operations) or len(approvals) != len(body.approvals):
        reject("Duplicate operation or approval identity")
    if len({a.id for a in body.artifacts}) != len(body.artifacts):
        reject("Duplicate artifact identity")
    if len({a.id for a in body.approvals}) != len(body.approvals):
        reject("Duplicate approval identity")
    if [e.sequence for e in body.events] != list(range(1, body.task.sequence + 1)):
        reject("Event sequence contains a gap, duplicate or reorder")
    if not body.events or body.events[0].type != "task.created":
        reject("Missing task creation event")
    for op in body.operations:
        if (
            set(op.request) != {"tool", "resource", "parameters"}
            or digest(op.request) != op.request_digest
        ):
            reject("Recorded request digest mismatch")
        if (
            not isinstance(op.request["tool"], str)
            or not isinstance(op.request["resource"], str)
            or not isinstance(op.request["parameters"], dict)
        ):
            reject("Malformed recorded request")
    proposed, dispatched, final, decisions = set(), [], {}, {}
    for event in body.events:
        op_id = event.payload.get("operation_id")
        if op_id is not None and (not isinstance(op_id, str) or op_id not in ops):
            reject("Event references an unknown operation")
        if event.type == "operation.proposed":
            if op_id is None or op_id in proposed:
                reject("Invalid operation proposal event")
            proposed.add(op_id)
            final[op_id] = "NOT_SUBMITTED"
        elif event.type == "approval.decided":
            if op_id not in proposed or event.payload.get("decision") not in (
                "APPROVED",
                "REJECTED",
            ):
                reject("Invalid approval decision event")
            decisions[op_id] = event.payload["decision"]
        elif event.type == "operation.dispatched":
            if op_id not in proposed or op_id in dispatched:
                reject("Invalid operation dispatch order")
            dispatched.append(op_id)
            if (
                ops[op_id].request.get("tool") in APPROVAL_TOOLS
                and decisions.get(op_id) != "APPROVED"
            ):
                reject("Protected dispatch precedes approval")
            final[op_id] = "PENDING"
        elif event.type in {"operation.succeeded", "operation.failed", "operation.unknown"}:
            if op_id not in proposed:
                reject("Outcome without proposal")
            final[op_id] = event.type.split(".")[1].upper()
    if proposed != set(ops):
        reject("Operations do not match proposal events")
    for approval in body.approvals:
        if (
            approval.operation_id not in ops
            or approval.payload_digest != ops[approval.operation_id].payload_digest
        ):
            reject("Approval digest does not match operation")
        if (
            approval.operation_id in decisions
            and approval.status != decisions[approval.operation_id]
        ):
            reject("Approval status disagrees with decision history")
    for op in body.operations:
        if op.attempts != int(op.id in dispatched):
            reject("Dispatch count disagrees with event history")
        if final.get(op.id) != op.status:
            reject("Operation outcome disagrees with event history")
        if op.status in {"PENDING", "UNKNOWN", "SUCCEEDED"} and op.id not in dispatched:
            reject("Active or successful operation without dispatch")
        if op.status == "SUCCEEDED" and (not op.result or op.result.get("confirmed") is not True):
            reject("Success requires a recorded confirmation")
        if op.id in dispatched and op.request["tool"] in APPROVAL_TOOLS:
            approval = approvals.get(op.id)
            if not approval or approval.status != "APPROVED":
                reject("Protected operation lacks recorded approval")
    terminated = [e for e in body.events if e.type == "task.terminated"]
    if body.task.status == "TERMINATED":
        if len(terminated) != 1 or terminated[0].payload.get("result") != body.task.result:
            reject("Task termination disagrees with event history")
        if any(o.status in {"PENDING", "UNKNOWN"} for o in body.operations):
            reject("Terminated task contains unresolved operations")
        if body.task.result == "SUCCESS" and (
            any(o.status != "SUCCEEDED" for o in body.operations)
            or not any(a.kind == "verification" for a in body.artifacts)
        ):
            reject("Success lacks confirmed operations or verification manifest")
    elif terminated or body.task.result is not None:
        reject("Nonterminal task contains a terminal result")
    return {
        "schema": "recording-v2",
        "task_id": body.task.id,
        "tenant": body.task.tenant,
        "event_count": len(body.events),
        "operation_count": len(ops),
        "dispatch_order": dispatched,
        "unresolved": [o.id for o in body.operations if o.status in {"PENDING", "UNKNOWN"}],
        "fully_confirmed_dispatches": all(ops[id].status == "SUCCEEDED" for id in dispatched),
        "integrity_checked": True,
        "remote_state_verified": False,
        "artifact_contents_verified": False,
    }


def export_audit(service, principal, task_id):
    with service.db.snapshot(principal.tenant_id) as s:
        task = tenant_get(s, Task, task_id, principal.tenant_id)
        authorize(s, principal, task)
        if (
            task.contract.get("workflow") == "repair_candidate"
            and principal.subject != task.principal
            and "operator" not in principal.roles
        ):
            raise DomainError(
                "REPAIR_REVIEW_FORBIDDEN", "Repair export requires owner or operator", 403
            )

        def rows(model, condition, maximum, order):
            values = s.scalars(
                select(model)
                .where(model.tenant_id == principal.tenant_id, condition)
                .order_by(order)
                .limit(maximum + 1)
            ).all()
            if len(values) > maximum:
                raise DomainError("RECORDING_LIMIT", "Task exceeds audit export limit", 413)
            return values

        operations = rows(Operation, Operation.task_id == task_id, 100, Operation.id)
        events = rows(TaskEvent, TaskEvent.task_id == task_id, 10000, TaskEvent.sequence)
        approvals = rows(
            Approval, Approval.operation_id.in_([o.id for o in operations]), 100, Approval.id
        )
        artifacts = rows(Artifact, Artifact.task_id == task_id, 1000, Artifact.id)
        order = {
            e.payload["operation_id"]: e.sequence
            for e in events
            if e.event_type == "operation.proposed"
        }
        operations.sort(key=lambda o: (order.get(o.id, 0), o.id))
        body = {
            "schema": "recording-v2",
            "release": task.contract["release_id"],
            "mode": service.settings.execution_mode,
            "task": {
                "id": task.id,
                "tenant": principal.tenant_id,
                "contract": task.contract,
                "version": task.version,
                "status": task.status,
                "result": task.result,
                "sequence": task.next_sequence,
            },
            "operations": [
                {
                    "id": o.id,
                    "request": request,
                    "request_digest": digest(request),
                    "payload_digest": o.payload_digest,
                    "status": o.status,
                    "attempts": o.attempts,
                    "result": o.result,
                }
                for o in operations
                for request in [
                    {"tool": o.tool, "resource": o.resource, "parameters": o.parameters}
                ]
            ],
            "approvals": [
                {
                    "id": a.id,
                    "operation_id": a.operation_id,
                    "payload_digest": a.payload_digest,
                    "status": a.status,
                }
                for a in approvals
            ],
            "events": [
                {"sequence": e.sequence, "type": e.event_type, "payload": e.payload} for e in events
            ],
            "artifacts": [{"id": a.id, "kind": a.kind, "digest": a.digest} for a in artifacts],
        }
    # Catch grant revocation that committed while the consistent snapshot was being read.
    service.get_task(principal, task_id)
    recording = {"body": body, "digest": digest(body)}
    check_recording(recording)
    return recording
