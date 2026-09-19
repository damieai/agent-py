"""Deterministic simulation harness for both end-to-end tracks.

Real model decisions have a separate explicit gateway; fixture findings are never labeled
as model-generated or as live enterprise verification.
"""

import json

from sqlalchemy import select

from agent_py.artifacts import ArtifactStore
from agent_py.db import Approval, Operation, Task, now, tenant_get
from agent_py.domain import ActionProposal, DomainError, Principal, digest
from agent_py.service import aware


class SimulationHarness:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.artifacts = ArtifactStore(self.db, service.settings.artifact_root)

    def tick(self, tenant: str, task_id: str) -> dict:
        if self.service.settings.execution_mode != "simulation":
            raise DomainError("HARNESS_MODE", "Simulation harness cannot access live mode", 403)
        with self.db.session(tenant) as s:
            task = tenant_get(s, Task, task_id, tenant)
            if task.status == "TERMINATED":
                return {"done": True, "status": task.status, "result": task.result}
            operations = list(
                s.scalars(
                    select(Operation)
                    .where(Operation.tenant_id == tenant, Operation.task_id == task_id)
                    .order_by(Operation.created_at)
                )
            )
        if task.taken_over:
            return {"done": False, "wait": "HUMAN_TAKEOVER"}
        for op in operations:
            if op.status in {"PENDING", "UNKNOWN"}:
                refreshed = self.service.reconcile(tenant, op.id)
                if refreshed.status == "UNKNOWN":
                    return {"done": False, "wait": "RECONCILIATION"}
        if task.cancelled:
            self.service.finish(tenant, task_id)
            return {"done": True, "result": "CANCELLED"}
        with self.db.session(tenant) as s:
            self.service._executable(s, tenant_get(s, Task, task_id, tenant))
        contract = task.contract
        p = Principal(
            tenant_id=tenant,
            subject=task.principal,
            roles=["developer", "operator"],
            projects=[contract["project"]],
            environments=[contract["environment"]],
        )
        if not operations:
            report = {
                "simulation": True,
                "kind": contract["kind"],
                "task_id": task_id,
                "findings": [
                    "Fixture: boundary-condition defect"
                    if contract["kind"] == "repair"
                    else "Fixture: deployment reduced worker capacity"
                ],
                "evidence": ["fixture://demo-service/baseline", "fixture://metrics/queue-depth"],
                "limitations": "Synthetic evidence; no live repository, model, or cluster was accessed.",
            }
            self.artifacts.put(tenant, task_id, "investigation", json.dumps(report).encode())
        state = self.service.remote.snapshot(tenant, contract["resource"])
        candidate = digest("fixed-candidate")
        sequence = (
            [
                ("create_pr", {"candidate_sha": candidate, "base_sha": digest("baseline")}),
                ("trigger_ci", {"candidate_sha": candidate, "job": "verify-candidate"}),
                ("merge_pr", {"source_sha": candidate, "target_sha": digest("baseline")}),
                (
                    "deploy",
                    {
                        "image_digest": "sha256:" + digest("healthy"),
                        "expected_revision": state["revision"],
                    },
                ),
            ]
            if contract["kind"] == "repair"
            else [
                ("runbook", {"replicas": 3, "expected_revision": state["revision"]}),
                (
                    "rollback",
                    {
                        "image_digest": "sha256:" + digest("healthy"),
                        "expected_revision": state["revision"],
                    },
                ),
            ]
        )
        for index, (tool, parameters) in enumerate(sequence):
            step = f"v1:{index}:{tool}"
            with self.db.session(tenant) as s:
                op = s.scalar(
                    select(Operation).where(
                        Operation.tenant_id == tenant,
                        Operation.task_id == task_id,
                        Operation.step_key == step,
                    )
                )
                if op and op.status == "SUCCEEDED":
                    continue
                if op and op.status == "FAILED":
                    self.service.finish(tenant, task_id)
                    return {"done": True, "result": "FAILED"}
            if not op:
                op = self.service.propose(
                    p,
                    task_id,
                    step,
                    ActionProposal(tool=tool, resource=contract["resource"], parameters=parameters),
                )
            try:
                result = self.service.execute(tenant, op.id)
            except DomainError as exc:
                if exc.code == "APPROVAL_REQUIRED":
                    with self.db.session(tenant) as s:
                        approval = s.scalar(
                            select(Approval).where(
                                Approval.tenant_id == tenant, Approval.operation_id == op.id
                            )
                        )
                    if approval.status == "REJECTED" or aware(approval.expires_at) <= now():
                        reason = (
                            "APPROVAL_REJECTED"
                            if approval.status == "REJECTED"
                            else "APPROVAL_EXPIRED"
                        )
                        self.service._record(tenant, op.id, "FAILED", None, reason)
                        self.service.finish(tenant, task_id)
                        return {"done": True, "result": "FAILED"}
                    return {"done": False, "wait": "APPROVAL", "approval_id": approval.id}
                raise
            return {"done": False, "operation_id": op.id, "status": result.status}
        final = self.service.remote.snapshot(tenant, contract["resource"])
        if not final["healthy"]:
            raise DomainError("VERIFICATION_FAILED", "Authority reports an unhealthy resource")
        self.artifacts.put(
            tenant,
            task_id,
            "verification",
            json.dumps({"simulation": True, "verified": final["healthy"], "state": final}).encode(),
        )
        completed = self.service.finish(tenant, task_id)
        return {"done": completed.status == "TERMINATED", "result": completed.result}
