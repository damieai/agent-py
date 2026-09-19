from datetime import timedelta

import pytest
from sqlalchemy import func, select

from agent_py.db import Approval, Operation, Policy, Task, TaskEvent, now
from agent_py.domain import DomainError
from agent_py.harness import SimulationHarness
from agent_py.runtime import reconcile_once


def test_cancel_cannot_be_undone_by_takeover_or_resume(env, task):
    service, p, _ = env
    service.stop(p, task.id)
    service.stop(p, task.id, takeover=True)
    current = service.get_task(p, task.id)
    assert current.cancelled and not current.taken_over
    with pytest.raises(DomainError, match="cannot resume"):
        service.resume(p, task.id, current.version)


def test_resume_rechecks_version_role_and_keeps_budget_and_deadline(env, task):
    service, p, _ = env
    service.stop(p, task.id, takeover=True)
    current = service.get_task(p, task.id)
    with pytest.raises(DomainError, match="version"):
        service.resume(p, task.id, task.version)
    with pytest.raises(DomainError, match="operator"):
        service.resume(p.model_copy(update={"roles": ["developer"]}), task.id, current.version)
    resumed = service.resume(p, task.id, current.version)
    assert resumed.version == current.version + 1 and not resumed.taken_over
    assert resumed.deadline == current.deadline and resumed.spent == current.spent
    assert resumed.contract == current.contract
    assert SimulationHarness(service).tick("t1", task.id)["status"] == "SUCCEEDED"


@pytest.mark.parametrize("reason", ["deadline", "emergency"])
def test_resume_does_not_bypass_runtime_policy(env, task, reason):
    service, p, _ = env
    service.stop(p, task.id, takeover=True)
    with service.db.session("t1") as s:
        if reason == "deadline":
            s.get(Task, task.id).deadline = now() - timedelta(seconds=1)
        else:
            s.add(Policy(tenant_id="t1", stopped=True))
    with pytest.raises(DomainError):
        service.resume(p, task.id, service.get_task(p, task.id).version)
    assert service.get_task(p, task.id).taken_over


def test_expired_approval_ends_task_without_side_effect(env, task):
    service = env[0]
    harness = SimulationHarness(service)
    for _ in range(5):
        result = harness.tick("t1", task.id)
        if result.get("wait") == "APPROVAL":
            break
    with service.db.session("t1") as s:
        approval = s.get(Approval, result["approval_id"])
        approval.expires_at = now() - timedelta(seconds=1)
        op_id = approval.operation_id
    assert harness.tick("t1", task.id) == {"done": True, "result": "FAILED"}
    with service.db.session("t1") as s:
        op = s.get(Operation, op_id)
        assert op.attempts == 0 and op.error == "APPROVAL_EXPIRED"


def test_uncertain_escalation_is_once_and_does_not_override_takeover(env, task):
    from test_execution import proposed

    service, p, _ = env
    op = proposed(env, task)
    service._record("t1", op.id, "UNKNOWN", None, "LOST")
    old = now() - timedelta(minutes=16)
    with service.db.session("t1") as s:
        s.get(Operation, op.id).updated_at = old
    service.stop(p, task.id, takeover=True)
    reconcile_once(service, "t1")
    reconcile_once(service, "t1")
    with service.db.session("t1") as s:
        assert s.get(Operation, op.id).recovery_status == "MANUAL_REVIEW"
        assert (
            s.scalar(
                select(func.count())
                .select_from(TaskEvent)
                .where(TaskEvent.event_type == "operation.escalated")
            )
            == 1
        )
    assert service.get_task(p, task.id).waiting_reason == "HUMAN_TAKEOVER"
    service._record("t1", op.id, "SUCCEEDED", {"confirmed": True, "external_id": "late"})
    with service.db.session("t1") as s:
        assert s.get(Operation, op.id).recovery_status is None


def test_resume_api_requires_current_version(env, task):
    from fastapi.testclient import TestClient

    from agent_py.api import create_app
    from agent_py.security import issue_dev_token

    service, p, _ = env
    client = TestClient(create_app(service.settings, service.db, service.remote))
    headers = {"Authorization": "Bearer " + issue_dev_token(service.settings, p)}
    taken = client.post(f"/api/v1/tasks/{task.id}/takeover", headers=headers).json()
    resumed = client.post(
        f"/api/v1/tasks/{task.id}/resume",
        headers=headers,
        json={"expected_version": taken["version"]},
    )
    assert resumed.status_code == 200 and not resumed.json()["taken_over"]
    assert (
        client.post(
            f"/api/v1/tasks/{task.id}/resume",
            headers=headers,
            json={"expected_version": taken["version"]},
        ).status_code
        == 409
    )


def test_blocked_workflow_stays_alive_without_duplicate_events(env, task):
    import asyncio

    from agent_py.runtime import Activities

    service = env[0]
    service.settings.execution_mode = "live"
    activities = Activities(service)
    for _ in range(2):
        result = asyncio.run(activities.tick({"tenant": "t1", "task_id": task.id}))
        assert result["done"] is False and result["wait"] == "MODEL_API_DISABLED"
    with service.db.session("t1") as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(TaskEvent)
                .where(TaskEvent.event_type == "task.blocked")
            )
            == 1
        )


def test_pending_uncertainty_age_survives_first_reconciliation(env, task):
    from test_execution import proposed

    service = env[0]
    op = proposed(env, task)
    with service.db.session("t1") as s:
        current = s.get(Operation, op.id)
        current.status = "PENDING"
        current.updated_at = now() - timedelta(minutes=16)
    reconcile_once(service, "t1")
    with service.db.session("t1") as s:
        current = s.get(Operation, op.id)
        assert current.status == "UNKNOWN" and current.recovery_status == "MANUAL_REVIEW"
