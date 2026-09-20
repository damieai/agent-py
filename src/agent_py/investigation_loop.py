"""At most three paid read-only decisions, with immutable per-round retrieval inputs."""

import json

from sqlalchemy import select

from agent_py.artifacts import ArtifactStore
from agent_py.context import ContextBundle, ContextCompiler
from agent_py.db import InvestigationRound, Reservation, Task, emit, now, tenant_get, uid
from agent_py.domain import DomainError, Principal
from agent_py.model import AnthropicGateway
from agent_py.repair import insert_once

MAX_ROUNDS = 3


def normalized(query):
    return " ".join(query.casefold().split())


class BoundedInvestigation:
    def __init__(self, service, gateway=None):
        self.service, self.gateway = service, gateway

    def tick(self, task):
        service = self.service
        tenant, task_id, contract = task.tenant_id, task.id, task.contract
        principal = Principal(
            tenant_id=tenant,
            subject=task.principal,
            roles=[],
            projects=[contract["project"]],
            environments=[contract["environment"]],
        )
        compiler = ContextCompiler(service.db, service.settings.context_strategy)
        history, bundles, queries = [], [], []
        query = contract["goal"]
        gateway = self.gateway

        def validate_all():
            for bundle in bundles:
                compiler.validate(
                    principal,
                    contract["project"],
                    contract["environment"],
                    bundle,
                    task_id=task_id,
                )
            with service.db.session(tenant) as s:
                service._executable(s, tenant_get(s, Task, task_id, tenant))

        reason = "ROUND_LIMIT"
        for ordinal in range(1, MAX_ROUNDS + 1):
            with service.db.session(tenant) as s:
                snapshot = s.scalar(
                    select(InvestigationRound).where(
                        InvestigationRound.tenant_id == tenant,
                        InvestigationRound.task_id == task_id,
                        InvestigationRound.ordinal == ordinal,
                    )
                )
            if snapshot is None:
                validate_all()
                if ordinal == 1 and service.settings.collection_manifest is not None:
                    from agent_py.collection import CollectionManifest, EvidenceCollector

                    EvidenceCollector(
                        service, CollectionManifest.load(service.settings.collection_manifest)
                    ).collect(principal, task_id)
                bundle = compiler.compile(
                    principal,
                    contract["project"],
                    contract["environment"],
                    query,
                    task_id=task_id,
                )
                if not bundle.documents and not history:
                    with service.db.session(tenant) as s:
                        current = tenant_get(s, Task, task_id, tenant, True)
                        service._executable(s, current)
                        current.status, current.waiting_reason = "WAITING", "EVIDENCE_REQUIRED"
                    return {"done": False, "wait": "EVIDENCE_REQUIRED"}
                # The unique ordinal arbitrates competing workers before any paid call.
                with service.db.session(tenant) as s:
                    current = tenant_get(s, Task, task_id, tenant, True)
                    service._executable(s, current)
                    insert_once(
                        s,
                        InvestigationRound,
                        dict(
                            id=uid(),
                            tenant_id=tenant,
                            task_id=task_id,
                            created_at=now(),
                            ordinal=ordinal,
                            query=query,
                            context=bundle.as_dict(),
                        ),
                        ["tenant_id", "task_id", "ordinal"],
                    )
                    snapshot = s.scalar(
                        select(InvestigationRound).where(
                            InvestigationRound.tenant_id == tenant,
                            InvestigationRound.task_id == task_id,
                            InvestigationRound.ordinal == ordinal,
                        )
                    )
            if snapshot.query != query:
                raise DomainError("INVESTIGATION_DRIFT", "Persisted investigation query changed")
            bundle = ContextBundle(**snapshot.context)
            if not bundle.documents:
                reason = "NO_EVIDENCE"
                break
            if any(bundle.digest == prior.digest for prior in bundles):
                reason = "NO_PROGRESS"
                break
            bundles.append(bundle)
            queries.append(query)
            validate_all()
            call_key = f"investigation-loop:v1:{ordinal}"
            with service.db.session(tenant) as s:
                saved = s.scalar(
                    select(Reservation).where(
                        Reservation.tenant_id == tenant,
                        Reservation.task_id == task_id,
                        Reservation.call_key == call_key,
                    )
                )
                recovered = saved is not None and saved.decision is not None
            gateway = gateway or AnthropicGateway(
                service.settings,
                service,
                service.settings.model_input_micro_per_token,
                service.settings.model_output_micro_per_token,
            )
            decision = gateway.investigate(
                tenant,
                task_id,
                call_key,
                contract["goal"],
                {
                    **bundle.as_dict(),
                    "round": ordinal,
                    "max_rounds": MAX_ROUNDS,
                    "query": query,
                    "previous_decisions": history,
                },
            )
            validate_all()
            history.append(decision.model_dump())
            if decision.stop:
                reason = "MODEL_STOP"
                break
            if normalized(decision.next_query) in [normalized(q) for q in queries]:
                reason = "REPEATED_QUERY"
                break
            query = decision.next_query
            if ordinal < MAX_ROUNDS and not recovered:
                # Yield after one new decision: cancellation and worker leases run each tick.
                return {"done": False, "wait": "INVESTIGATION_CONTINUE", "round": ordinal}

        validate_all()
        report = {
            "schema": "investigation-loop/v1",
            "rounds": [
                {"query": q, "context": b.as_dict(), "decision": d}
                for q, b, d in zip(queries, bundles, history)
            ],
            "stop_reason": reason,
            "max_rounds": MAX_ROUNDS,
            "model": service.settings.model_id,
            "executed": False,
            "scope": "authorized_evidence_readonly",
            "requires_human_review": True,
        }
        artifact = ArtifactStore(service.db, service.settings.artifact_root).put(
            tenant, task_id, "model-analysis", json.dumps(report, sort_keys=True).encode()
        )
        with service.db.session(tenant) as s:
            current = tenant_get(s, Task, task_id, tenant, True)
            service._executable(s, current)
            if current.waiting_reason != "HUMAN_REVIEW":
                current.status, current.waiting_reason = "WAITING", "HUMAN_REVIEW"
                emit(
                    s,
                    current,
                    "investigation.completed",
                    {
                        "artifact_id": artifact.id,
                        "rounds": len(history),
                        "stop_reason": reason,
                    },
                )
        return {"done": False, "wait": "HUMAN_REVIEW", "artifact_id": artifact.id}
