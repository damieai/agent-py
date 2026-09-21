"""Control transitions reflect committed state; rollback stays on the action ledger."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

pytest.importorskip("langfuse")
from test_execution import approve, proposed
from test_langfuse import decode
from test_trace_context import configure

from agent_py.db import Operation, TaskEvent, now
from agent_py.domain import ActionProposal, DomainError, digest

PREFIX = "langfuse.observation.metadata."


def decoded(service, requests, name):
    service.telemetry.close()
    return [a for span, a, _ in decode(requests) if span.name == name]


def test_cancel_waits_for_uncertainty_and_duplicate_finish_is_not_new_transition(
    env, task, monkeypatch
):
    requests, _ = configure(env, monkeypatch)
    service, principal, _ = env
    try:
        op = proposed(env, task)
        service.remote.inject("t1", op.id, "response_lost")
        service.execute("t1", op.id)
        assert service.stop(principal, task.id) is None
        service.stop(principal, task.id)
        waiting = service.finish("t1", task.id)
        assert waiting.cancelled and waiting.status == "WAITING" and waiting.result is None
        service.reconcile("t1", op.id)
        assert service.finish("t1", task.id).result == "CANCELLED"
        service.finish("t1", task.id)
        cancels = decoded(service, requests, "task.cancel")
        finishes = decoded(service, requests, "task.finish")
        assert [a[PREFIX + "changed"] for a in cancels] == [True, False]
        assert finishes[0][PREFIX + "waiting_reason"] == "RECONCILIATION"
        assert PREFIX + "task_result" not in finishes[0]
        assert finishes[1][PREFIX + "task_result"] == "CANCELLED"
        assert [a[PREFIX + "changed"] for a in finishes] == [True, True, False]
        assert service.remote.snapshot("t1", op.resource)["effect_count"] == 1
        with service.db.session("t1") as s:
            assert (
                s.scalar(
                    select(func.count())
                    .select_from(TaskEvent)
                    .where(TaskEvent.event_type == "task.terminated")
                )
                == 1
            )
    finally:
        service.telemetry.close()


def test_takeover_resume_preserves_policy_and_records_noop_or_error(env, task, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service, principal, _ = env
    try:
        service.stop(principal, task.id, takeover=True)
        service.stop(principal, task.id, takeover=True)
        current = service.get_task(principal, task.id)
        assert service.finish("t1", task.id).taken_over
        with pytest.raises(DomainError):
            service.resume(principal, task.id, task.version)
        resumed = service.resume(principal, task.id, current.version)
        service.resume(principal, task.id, resumed.version)
        assert resumed.deadline == current.deadline and resumed.spent == current.spent
        takeovers = decoded(service, requests, "task.takeover")
        resumes = decoded(service, requests, "task.resume")
        assert [a[PREFIX + "changed"] for a in takeovers] == [True, False]
        assert resumes[0][PREFIX + "outcome"] == "error"
        assert PREFIX + "task_status" not in resumes[0]
        assert [a[PREFIX + "changed"] for a in resumes[1:]] == [True, False]
        assert resumes[1][PREFIX + "task_status"] == "QUEUED"
        assert resumes[1][PREFIX + "version"] == resumed.version
        assert not resumes[1][PREFIX + "taken_over"]
        assert decoded(service, requests, "task.finish")[0][PREFIX + "changed"] is False
    finally:
        service.telemetry.close()


@pytest.mark.parametrize("takeover", [False, True])
def test_concurrent_escalation_emits_once_preserving_control_state(
    env, task, monkeypatch, takeover
):
    requests, _ = configure(env, monkeypatch)
    service, principal, _ = env
    try:
        op = proposed(env, task)
        service.remote.inject("t1", op.id, "response_lost")
        service.execute("t1", op.id)
        with service.db.session("t1") as s:
            s.get(Operation, op.id).updated_at = now() - timedelta(minutes=16)
        service.stop(principal, task.id, takeover=takeover)
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert (
                list(pool.map(lambda _: service.escalate_uncertain("t1", op.id), range(4)))
                == [None] * 4
            )
        observations = decoded(service, requests, "operation.escalate")
        assert len(observations) == 1
        attrs = observations[0]
        assert attrs[PREFIX + "recovery_status"] == "MANUAL_REVIEW"
        assert attrs[PREFIX + "status"] == "UNKNOWN"
        assert attrs[PREFIX + "taken_over"] == takeover
        assert attrs[PREFIX + "cancelled"] != takeover
        assert attrs[PREFIX + "task_status"] == ("WAITING" if takeover else "CANCELLING")
        assert (
            len(
                {
                    a[PREFIX + "operation_id"]
                    for a in observations + decoded(service, requests, "operation.execute")
                }
            )
            == 1
        )
    finally:
        service.telemetry.close()


def test_rollback_approval_lost_response_and_reuse_share_identity(env, task, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service, principal, _ = env
    try:
        op = service.propose(
            principal,
            task.id,
            "recover",
            ActionProposal(
                tool="rollback",
                resource="demo-service",
                parameters={"image_digest": "sha256:" + digest("healthy"), "expected_revision": 1},
            ),
        )
        with pytest.raises(DomainError, match="approval"):
            service.execute("t1", op.id)
        approve(env, op)
        service.remote.inject("t1", op.id, "response_lost")
        assert service.execute("t1", op.id).status == "UNKNOWN"
        assert service.reconcile("t1", op.id).status == "SUCCEEDED"
        service.execute("t1", op.id)
        names = [
            "operation.propose",
            "operation.approval",
            "operation.execute",
            "operation.query",
            "operation.result_reused",
        ]
        rows = [decoded(service, requests, name) for name in names]
        assert all(len(row) == 1 for row in rows)
        assert all(row[0][PREFIX + "action_kind"] == "rollback" for row in rows)
        assert len({row[0][PREFIX + "operation_id"] for row in rows}) == 1
        assert service.remote.snapshot("t1", op.resource)["effect_count"] == 1
        assert b"image_digest" not in b"".join(r.content for r in requests)
    finally:
        service.telemetry.close()


def test_failed_verification_does_not_claim_terminal_result(env, task, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    try:
        with pytest.raises(DomainError, match="verification"):
            service.finish("t1", task.id)
        attrs = decoded(service, requests, "task.finish")[0]
        assert attrs[PREFIX + "outcome"] == "error"
        assert PREFIX + "task_result" not in attrs
        assert not attrs[PREFIX + "changed"]
        assert service.get_task(env[1], task.id).status != "TERMINATED"
    finally:
        service.telemetry.close()


def test_escalation_commit_failure_emits_no_transition(env, task, monkeypatch):
    from contextlib import contextmanager

    requests, _ = configure(env, monkeypatch)
    service = env[0]
    original = service.db.session
    try:
        op = proposed(env, task)
        service._record("t1", op.id, "UNKNOWN", None)
        with original("t1") as s:
            s.get(Operation, op.id).updated_at = now() - timedelta(minutes=16)

        @contextmanager
        def failed(tenant):
            with original(tenant) as s:
                yield s
                raise RuntimeError("PRIVATE_COMMIT_ERROR")

        monkeypatch.setattr(service.db, "session", failed)
        with pytest.raises(RuntimeError):
            service.escalate_uncertain("t1", op.id)
        monkeypatch.setattr(service.db, "session", original)
        assert decoded(service, requests, "operation.escalate") == []
        with original("t1") as s:
            assert s.get(Operation, op.id).recovery_status == "RECONCILING"
        assert b"PRIVATE" not in b"".join(r.content for r in requests)
    finally:
        service.telemetry.close()


def test_export_failure_does_not_prevent_cancel(env, task, monkeypatch):
    _, processors = configure(env, monkeypatch)
    service = env[0]

    def fail(_):
        raise RuntimeError("PRIVATE_EXPORT_ERROR")

    monkeypatch.setattr(processors[0], "sanitize", fail)
    try:
        assert service.stop(env[1], task.id) is None
        assert service.get_task(env[1], task.id).cancelled
        assert service.telemetry.langfuse_events.labels("sanitization_failed")._value.get() == 1
    finally:
        service.telemetry.close()
