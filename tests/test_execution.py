from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select

from agent_py.db import Approval, Grant, Operation, Policy, now
from agent_py.domain import ActionProposal, DomainError, TaskContract, digest


def proposed(env, task, tool="create_pr", step="step-1"):
    service, p, _ = env
    params = {"candidate_sha": digest("candidate"), "base_sha": digest("base")}
    if tool == "deploy":
        params = {"image_digest": "sha256:" + digest("image"), "expected_revision": 1}
    return service.propose(
        p, task.id, step, ActionProposal(tool=tool, resource="demo-service", parameters=params)
    )


def approve(env, op):
    service, _, reviewer = env
    with service.db.session("t1") as s:
        a = s.scalar(select(Approval).where(Approval.operation_id == op.id))
    return service.decide(reviewer, a.id, "approve", a.payload_digest)


def test_request_retries_and_parameter_conflict(env, task):
    service, p, _ = env
    same = service.create_task(p, TaskContract(**task.contract), "request-1")
    assert same.id == task.id
    with pytest.raises(DomainError, match="another request"):
        service.create_task(
            p, TaskContract(**{**task.contract, "goal": "Another request"}), "request-1"
        )


def test_cross_tenant_access(env, task):
    service, p, _ = env
    with pytest.raises(DomainError) as exc:
        service.get_task(p.model_copy(update={"tenant_id": "t2"}), task.id)
    assert exc.value.status == 404


def test_operation_conflict(env, task):
    op = proposed(env, task)
    assert proposed(env, task).id == op.id
    service, p, _ = env
    with pytest.raises(DomainError, match="another action"):
        service.propose(
            p,
            task.id,
            "step-1",
            ActionProposal(
                tool="trigger_ci",
                resource="demo-service",
                parameters={"candidate_sha": digest("candidate"), "job": "verify-candidate"},
            ),
        )


def test_response_loss_and_lag_do_not_duplicate(env, task):
    service, _, _ = env
    op = proposed(env, task)
    service.remote.inject("t1", op.id, "response_lost")
    assert service.execute("t1", op.id).status == "UNKNOWN"
    service.remote.inject("t1", op.id, "query_hidden", remaining=2)
    for _ in range(2):
        assert service.reconcile("t1", op.id).status == "UNKNOWN"
        assert service.execute("t1", op.id).attempts == 1
    assert service.reconcile("t1", op.id).status == "SUCCEEDED"
    assert service.remote.snapshot("t1", op.resource)["effect_count"] == 1


def test_worker_dies_after_remote_commit_before_local_result(env, task):
    service, _, _ = env
    op = proposed(env, task)
    with service.db.session("t1") as s:
        row = s.get(Operation, op.id)
        row.status = "PENDING"
    service.remote.execute("t1", op.id, op.tool, op.resource, op.parameters)
    assert service.execute("t1", op.id).status == "PENDING"
    assert service.reconcile("t1", op.id).status == "SUCCEEDED"
    assert service.remote.snapshot("t1", op.resource)["effect_count"] == 1


def test_concurrent_dispatch(env, task):
    service, _, _ = env
    op = proposed(env, task)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: service.execute("t1", op.id), range(2)))
    service.reconcile("t1", op.id)
    assert service.remote.snapshot("t1", op.resource)["effect_count"] == 1


def test_approval_required_and_repeat_safe(env, task):
    service, _, reviewer = env
    op = proposed(env, task, "deploy")
    with pytest.raises(DomainError, match="approval"):
        service.execute("t1", op.id)
    a = approve(env, op)
    assert service.decide(reviewer, a.id, "approve", a.payload_digest).id == a.id
    assert service.execute("t1", op.id).status == "SUCCEEDED"
    assert service.execute("t1", op.id).attempts == 1


def test_expired_approval_blocks_dispatch(env, task):
    service, _, _ = env
    op = proposed(env, task, "deploy")
    a = approve(env, op)
    with service.db.session("t1") as s:
        s.get(Approval, a.id).expires_at = now() - timedelta(seconds=1)
    with pytest.raises(DomainError, match="approval"):
        service.execute("t1", op.id)


def test_revoked_approver_blocks_dispatch(env, task):
    service, _, _ = env
    op = proposed(env, task, "deploy")
    approve(env, op)
    with service.db.session("t1") as s:
        g = s.scalar(select(Grant).where(Grant.tenant_id == "t1", Grant.subject == "reviewer"))
        g.revoked = True
    with pytest.raises(DomainError, match="grant"):
        service.execute("t1", op.id)


def test_approval_does_not_override_remote_precondition(env, task):
    service, _, _ = env
    op = proposed(env, task, "deploy")
    approve(env, op)
    service.remote.execute(
        "t1", "manual-change", "runbook", op.resource, {"replicas": 3, "expected_revision": 1}
    )
    assert service.execute("t1", op.id).status == "FAILED"
    assert service.remote.snapshot("t1", op.resource)["effect_count"] == 1


def test_cancel_preserves_reconciliation(env, task):
    service, p, _ = env
    op = proposed(env, task)
    service.remote.inject("t1", op.id, "response_lost")
    service.execute("t1", op.id)
    service.stop(p, task.id)
    assert service.reconcile("t1", op.id).status == "SUCCEEDED"
    assert service.finish("t1", task.id).result == "CANCELLED"


def test_late_failure_never_overwrites_success(env, task):
    service, _, _ = env
    op = proposed(env, task)
    service.execute("t1", op.id)
    assert service._record("t1", op.id, "FAILED", None, "late").status == "SUCCEEDED"


def test_emergency_stop_preserves_queries(env, task):
    service, _, _ = env
    op = proposed(env, task)
    with service.db.session("t1") as s:
        s.add(Policy(tenant_id="t1", stopped=True))
    with pytest.raises(DomainError, match="disabled"):
        service.execute("t1", op.id)


def test_budget_reservation_and_settlement(env, task):
    service, _, _ = env
    r = service.reserve("t1", task.id, "call1", 600_000)
    with pytest.raises(DomainError, match="budget"):
        service.reserve("t1", task.id, "call2", 600_000)
    service.settle("t1", r.id, 100_000)
    service.settle("t1", r.id, 100_000)
    service.reserve("t1", task.id, "call2", 600_000)


def test_daily_budget_across_tasks(env, task):
    service, p, _ = env
    service.settings.daily_budget_micro_usd = 900_000
    service.reserve("t1", task.id, "call1", 600_000)
    second = service.create_task(p, TaskContract(**task.contract), "second")
    with pytest.raises(DomainError, match="Daily budget"):
        service.reserve("t1", second.id, "call1", 600_000)
