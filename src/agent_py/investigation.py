"""Persisted read-only investigation over explicitly imported, authorized evidence."""

import json

from sqlalchemy import select

from agent_py.artifacts import ArtifactStore
from agent_py.context import ContextCompiler
from agent_py.db import Reservation, Task, emit, tenant_get
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
            repair_candidate = task.contract.get("workflow") == "repair_candidate"
            if task.status == "TERMINATED":
                return {"done": True, "result": task.result}
            if task.taken_over:
                return {"done": False, "wait": "HUMAN_TAKEOVER"}
            if not task.cancelled:
                service._executable(s, task)
        if repair_candidate:
            from agent_py.repair import RepairHarness

            return RepairHarness(service, gateway=self.gateway).tick(tenant, task_id)
        if task.cancelled:
            service.finish(tenant, task_id)
            return {"done": True, "result": "CANCELLED"}
        if not service.settings.allow_model_api:
            raise DomainError("MODEL_API_DISABLED", "Explicit paid inference opt-in required", 403)
        if task.contract.get("workflow") == "investigation_loop":
            from agent_py.investigation_loop import BoundedInvestigation

            return BoundedInvestigation(service, self.gateway).tick(task)
        contract = task.contract
        principal = Principal(
            tenant_id=tenant,
            subject=task.principal,
            roles=[],
            projects=[contract["project"]],
            environments=[contract["environment"]],
        )
        compiler = ContextCompiler(
            service.db, service.settings.context_strategy, telemetry=service.telemetry
        )
        if service.settings.collection_manifest is not None:
            from agent_py.collection import CollectionManifest, EvidenceCollector

            with service.db.session(tenant) as s:
                started = s.scalar(
                    select(Reservation.id).where(
                        Reservation.tenant_id == tenant,
                        Reservation.task_id == task_id,
                        Reservation.call_key == "investigation:v1",
                    )
                )
            if not started:
                EvidenceCollector(
                    service, CollectionManifest.load(service.settings.collection_manifest)
                ).collect(principal, task_id)
        bundle = compiler.compile(
            principal,
            contract["project"],
            contract["environment"],
            contract["goal"],
            task_id=task_id,
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
        compiler.validate(
            principal, contract["project"], contract["environment"], bundle, task_id=task_id
        )
        decision = gateway.decide(
            tenant, task_id, "investigation:v1", contract["goal"], bundle.as_dict()
        )
        compiler.validate(
            principal, contract["project"], contract["environment"], bundle, task_id=task_id
        )
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
                return {
                    "done": current.status == "TERMINATED",
                    "wait": "TASK_STOPPED",
                    "artifact_id": artifact.id,
                }
            if current.waiting_reason != "HUMAN_REVIEW":
                current.status, current.waiting_reason = "WAITING", "HUMAN_REVIEW"
                emit(s, current, "investigation.completed", {"artifact_id": artifact.id})
        # Keep the workflow alive for cancellation/takeover; analysis is not business SUCCESS.
        return {"done": False, "wait": "HUMAN_REVIEW", "artifact_id": artifact.id}


def build_harness(service):
    if service.settings.execution_mode == "simulation":
        from agent_py.harness import SimulationHarness

        return SimulationHarness(service)
    return InvestigationHarness(service)
