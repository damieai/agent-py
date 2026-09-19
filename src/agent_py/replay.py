import json

from sqlalchemy import select

from agent_py.db import Operation, TaskEvent
from agent_py.domain import DomainError, digest


def export_recording(service, principal, task_id: str) -> dict:
    task = service.get_task(principal, task_id)
    with service.db.session(principal.tenant_id) as s:
        ops = s.scalars(
            select(Operation)
            .where(Operation.tenant_id == principal.tenant_id, Operation.task_id == task_id)
            .order_by(Operation.created_at)
        ).all()
        events = s.scalars(
            select(TaskEvent)
            .where(TaskEvent.tenant_id == principal.tenant_id, TaskEvent.task_id == task_id)
            .order_by(TaskEvent.sequence)
        ).all()
    # Operator must still review content for business-sensitive values before export outside tenant.
    body = {
        "schema": "recording-v1",
        "release": task.contract["release_id"],
        "mode": service.settings.execution_mode,
        "operations": [
            {
                "id": o.id,
                "request": {"tool": o.tool, "resource": o.resource, "parameters": o.parameters},
                "status": o.status,
                "result": o.result,
            }
            for o in ops
        ],
        "events": [
            {"sequence": e.sequence, "type": e.event_type, "payload": e.payload} for e in events
        ],
    }
    return {"body": body, "digest": digest(body)}


class ReplayExecutor:
    """No provider client, credentials, or network fallback exists in this executor."""

    def __init__(self, recording: dict):
        if recording.get("digest") != digest(recording.get("body")):
            raise DomainError("RECORDING_CORRUPTED", "Recording checksum mismatch", 422)
        if recording["body"].get("schema") != "recording-v1":
            raise DomainError("RECORDING_VERSION", "Unsupported recording version", 422)
        operations = recording["body"]["operations"]
        self.operations = {o["id"]: o for o in operations}
        if len(self.operations) != len(operations):
            raise DomainError("RECORDING_DUPLICATE", "Duplicate operation identity", 422)

    def execute(self, tenant, operation, tool, resource, parameters):
        op = self.operations.get(operation)
        if not op or op["request"] != {
            "tool": tool,
            "resource": resource,
            "parameters": parameters,
        }:
            raise DomainError("REPLAY_DIVERGED", "No matching recorded operation", 409)
        if op["status"] != "SUCCEEDED" or op["result"] is None:
            raise DomainError("REPLAY_INCOMPLETE", "No recorded confirmed result", 409)
        return json.loads(json.dumps(op["result"]))
