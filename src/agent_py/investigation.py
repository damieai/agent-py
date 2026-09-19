"""Persisted read-only investigation over explicitly imported, authorized evidence."""

import json

from agent_py.artifacts import ArtifactStore
from agent_py.context import ContextCompiler
from agent_py.db import Task, emit, tenant_get
from agent_py.domain import DomainError, Principal
from agent_py.model import AnthropicGateway


class InvestigationHarness:
    def __init__(self, service, gateway=None):
        self.service = service
        self.gateway = gateway

    def tick(self, tenant: str, task_id: str):
        service = self.service
        with service.db.session(tenant) as s:
            task = tenant_get(s, Task, task_id, tenant)
            if task.status == "TERMINATED":
                return {"done": True, "result": task.result}
            if task.taken_over:
                return {"done": True, "wait": "HUMAN_TAKEOVER"}
            if not task.cancelled:
                service._executable(s, task)
        if task.cancelled:
            service.finish(tenant, task_id)
            return {"done": True, "result": "CANCELLED"}
        if not service.settings.allow_model_api:
            raise DomainError("MODEL_API_DISABLED", "Explicit paid inference opt-in required", 403)
        contract = task.contract
        principal = Principal(
            tenant_id=tenant,
            subject=task.principal,
            roles=[],
            projects=[contract["project"]],
            environments=[contract["environment"]],
        )
        compiler = ContextCompiler(service.db)
        bundle = compiler.compile(
            principal, contract["project"], contract["environment"], contract["goal"]
        )
        if not bundle.documents:
            with service.db.session(tenant) as s:
                current = tenant_get(s, Task, task_id, tenant, True)
                if not current.cancelled and not current.taken_over:
                    current.status, current.waiting_reason = "WAITING", "EVIDENCE_REQUIRED"
            return {"done": False, "wait": "EVIDENCE_REQUIRED"}
        gateway = self.gateway or AnthropicGateway(
            service.settings,
            service,
            service.settings.model_input_micro_per_token,
            service.settings.model_output_micro_per_token,
        )
        decision = gateway.decide(
            tenant, task_id, "investigation:v1", contract["goal"], bundle.as_dict()
        )
        compiler.validate(principal, contract["project"], contract["environment"], bundle)
        report = {
            "context": bundle.as_dict(),
            "decision": decision.model_dump(),
            "model": service.settings.model_id,
            "executed": False,
            "scope": "imported_evidence_readonly",
            "requires_human_review": True,
        }
        # A crash before this write is recoverable from the validated inference ledger.
        artifact = ArtifactStore(service.db, service.settings.artifact_root).put(
            tenant, task_id, "model-analysis", json.dumps(report, sort_keys=True).encode()
        )
        with service.db.session(tenant) as s:
            current = tenant_get(s, Task, task_id, tenant, True)
            if current.cancelled or current.taken_over or current.status == "TERMINATED":
                return {"done": True, "wait": "TASK_STOPPED", "artifact_id": artifact.id}
            if current.waiting_reason != "HUMAN_REVIEW":
                current.status, current.waiting_reason = "WAITING", "HUMAN_REVIEW"
                emit(s, current, "investigation.completed", {"artifact_id": artifact.id})
        # Analysis is complete; business repair/recovery is not claimed as SUCCESS.
        return {"done": True, "wait": "HUMAN_REVIEW", "artifact_id": artifact.id}


def build_harness(service):
    if service.settings.execution_mode == "simulation":
        from agent_py.harness import SimulationHarness

        return SimulationHarness(service)
    return InvestigationHarness(service)
