"""Reproducible simulation conformance, explicitly not a model-quality benchmark."""

import json
from pathlib import Path

from sqlalchemy import select

from agent_py.db import Approval, Grant, Operation, uid
from agent_py.domain import Principal, TaskContract
from agent_py.harness import SimulationHarness


def dataset():
    families = [
        "repair",
        "ci_response_loss",
        "rejected_release",
        "incident",
        "incident_response_loss",
        "cancel_during_reconciliation",
    ]
    result = []
    for split, size in [("development", 60), ("calibration", 20), ("holdout", 40)]:
        for i in range(size):
            family = families[i % 6]
            result.append(
                {
                    "id": f"{split}-{i:03d}",
                    "split": split,
                    "family": family,
                    "kind": "repair" if i % 6 < 3 else "incident",
                    "fixture": f"fixture-{split}-{i:03d}",
                    "expected": "FAILED"
                    if family == "rejected_release"
                    else "CANCELLED"
                    if family == "cancel_during_reconciliation"
                    else "SUCCESS",
                }
            )
    return result


def run_case(service, case):
    tenant = "eval-" + uid()
    with service.db.session(tenant) as s:
        for subject in ["actor", "reviewer"]:
            s.add(Grant(tenant_id=tenant, subject=subject, project="demo", environment="lab"))
    p = Principal(
        tenant_id=tenant,
        subject="actor",
        roles=["developer", "operator"],
        projects=["demo"],
        environments=["lab"],
    )
    reviewer = p.model_copy(update={"subject": "reviewer", "roles": ["approver", "operator"]})
    task = service.create_task(
        p,
        TaskContract(
            kind=case["kind"],
            goal="Conformance " + case["family"],
            project="demo",
            resource=case["fixture"],
        ),
        case["id"],
    )
    harness = SimulationHarness(service)
    injected = False
    original = service.remote.execute

    def execute(*args, **kwargs):
        nonlocal injected
        should_inject = case["family"] in {"incident_response_loss", "cancel_during_reconciliation"}
        should_inject |= case["family"] == "ci_response_loss" and args[2] == "trigger_ci"
        if not injected and should_inject:
            service.remote.inject(tenant, args[1], "response_lost")
            injected = True
        return original(*args, **kwargs)

    service.remote.execute = execute
    try:
        for _ in range(30):
            outcome = harness.tick(tenant, task.id)
            if (
                case["family"] == "cancel_during_reconciliation"
                and outcome.get("status") == "UNKNOWN"
            ):
                service.stop(p, task.id)
            if outcome.get("wait") == "APPROVAL":
                with service.db.session(tenant) as s:
                    a = s.get(Approval, outcome["approval_id"])
                decision = "reject" if case["family"] == "rejected_release" else "approve"
                service.decide(reviewer, a.id, decision, a.payload_digest)
            if outcome.get("done"):
                break
    finally:
        service.remote.execute = original
    actual = service.get_task(p, task.id)
    with service.db.session(tenant) as s:
        ops = list(s.scalars(select(Operation).where(Operation.tenant_id == tenant)))
    confirmed = sum(o.status == "SUCCEEDED" for o in ops)
    effects = service.remote.snapshot(tenant, case["fixture"])["effect_count"]
    safe = confirmed == effects and all(o.attempts <= 1 for o in ops)
    return {
        "id": case["id"],
        "family": case["family"],
        "task_id": task.id,
        "expected": case["expected"],
        "actual": actual.result,
        "passed": actual.result == case["expected"] and safe,
        "confirmed_operations": confirmed,
        "remote_effects": effects,
        "simulation": True,
    }


def evaluate(service, split: str, output: Path):
    if service.settings.execution_mode != "simulation":
        raise ValueError("Conformance evaluation cannot run against live systems")
    selected = [c for c in dataset() if c["split"] == split]
    if not selected:
        raise ValueError("Unknown dataset split")
    results = [run_case(service, case) for case in selected]
    report = {
        "suite": "simulation-conformance-v1",
        "not_a_model_benchmark": True,
        "dataset_limitation": "Distinct fixture IDs share scenario templates. This suite "
        "cannot measure generalization or model quality.",
        "split": split,
        "passed": sum(r["passed"] for r in results),
        "total": len(results),
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2))
    return report
