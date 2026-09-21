"""Observe the real local action ledger/authority; do not certify live write adapters."""

from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("langfuse")
from test_execution import approve, proposed
from test_langfuse import decode
from test_trace_context import configure

from agent_py.adapters.simulation import DisabledLiveExecutor
from agent_py.domain import DomainError

PREFIX = "langfuse.observation.metadata."


def spans(service, requests, name):
    service.telemetry.close()
    return [(s, a) for s, a, _ in decode(requests) if s.name == name]


def test_lost_response_queries_reuse_and_pseudonyms(env, task, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    try:
        op = proposed(env, task)
        assert proposed(env, task).id == op.id
        service.remote.inject("t1", op.id, "response_lost")
        with service.telemetry.span("worker.tick", **{"tenant.id": "t1", "task.id": task.id}):
            assert service.execute("t1", op.id).status == "UNKNOWN"
            service.remote.inject("t1", op.id, "query_hidden")
            assert service.reconcile("t1", op.id).status == "UNKNOWN"
            assert service.execute("t1", op.id).attempts == 1
            assert service.reconcile("t1", op.id).status == "SUCCEEDED"
            assert service.reconcile("t1", op.id).status == "SUCCEEDED"
        executions = spans(service, requests, "operation.execute")
        queries = spans(service, requests, "operation.query")
        reused = spans(service, requests, "operation.result_reused")
        proposals = spans(service, requests, "operation.propose")
        assert len(executions) == len(reused) == 1
        assert [a[PREFIX + "status"] for _, a in executions + queries] == [
            "UNKNOWN",
            "UNKNOWN",
            "SUCCEEDED",
        ]
        assert [a[PREFIX + "changed"] for _, a in proposals] == [True, False]
        assert all(a[PREFIX + "attempt"] == 1 for _, a in executions + queries + reused)
        observations = executions + queries + reused + proposals
        assert len({a[PREFIX + "operation_id"] for _, a in observations}) == 1
        assert all(a[PREFIX + "execution_mode"] == "simulation" for _, a in observations)
        root = next(s for s, _, _ in decode(requests) if s.name == "worker.tick")
        assert all(s.parent_span_id == root.span_id for s, _ in executions + queries + reused)
        assert service.remote.snapshot("t1", op.resource)["effect_count"] == 1
        wire = b"".join(r.content for r in requests)
        assert op.id.encode() not in wire and task.id.encode() not in wire
        assert op.resource.encode() not in wire and b"candidate_sha" not in wire
    finally:
        service.telemetry.close()


def test_approval_denial_repeated_decision_and_single_dispatch(env, task, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    try:
        op = proposed(env, task, "deploy")
        with pytest.raises(DomainError, match="approval"):
            service.execute("t1", op.id)
        approve(env, op)
        approve(env, op)
        service.execute("t1", op.id)
        decisions = spans(service, requests, "operation.approval")
        assert [a[PREFIX + "changed"] for _, a in decisions] == [True, False]
        assert all(a[PREFIX + "decision"] == "APPROVED" for _, a in decisions)
        assert len(spans(service, requests, "operation.execute")) == 1
    finally:
        service.telemetry.close()


@pytest.mark.parametrize("mode,expected", [("reject", "FAILED"), ("invalid", "UNKNOWN")])
def test_export_uses_validated_ledger_state(env, task, monkeypatch, mode, expected):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    try:
        op = proposed(env, task)
        if mode == "reject":
            service.remote.inject("t1", op.id, "reject")
        else:
            monkeypatch.setattr(service.remote, "execute", lambda *a: {"external_id": "PRIVATE"})
        assert service.execute("t1", op.id).status == expected
        _, attrs = spans(service, requests, "operation.execute")[0]
        assert attrs[PREFIX + "status"] == expected
        assert attrs[PREFIX + "outcome"] == "completed"
        assert b"PRIVATE" not in b"".join(r.content for r in requests)
    finally:
        service.telemetry.close()


def test_failed_query_does_not_dispatch_or_change_uncertainty(env, task, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    try:
        op = proposed(env, task)
        service.remote.inject("t1", op.id, "response_lost")
        service.execute("t1", op.id)

        def fail(*args):
            raise ConnectionError("PRIVATE_RECEIPT_ERROR")

        monkeypatch.setattr(service.remote, "query", fail)
        with pytest.raises(ConnectionError):
            service.reconcile("t1", op.id)
        assert service.execute("t1", op.id).status == "UNKNOWN"
        _, attrs = spans(service, requests, "operation.query")[0]
        assert attrs[PREFIX + "outcome"] == "error"
        assert attrs[PREFIX + "status"] == "UNKNOWN"
        assert len(spans(service, requests, "operation.execute")) == 1
        assert b"PRIVATE" not in b"".join(r.content for r in requests)
    finally:
        service.telemetry.close()


def test_concurrent_dispatch_emits_only_one_execution(env, task, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    try:
        op = proposed(env, task)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: service.execute("t1", op.id), range(4)))
        assert len(spans(service, requests, "operation.execute")) == 1
        assert service.remote.snapshot("t1", op.resource)["effect_count"] == 1
    finally:
        service.telemetry.close()


def test_uncertified_live_adapter_has_no_execute_observation(env, task, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    try:
        op = proposed(env, task)
        service.settings.execution_mode = "live"
        service.remote = DisabledLiveExecutor()
        with pytest.raises(DomainError, match="certified"):
            service.execute("t1", op.id)
        assert spans(service, requests, "operation.execute") == []
    finally:
        service.telemetry.close()


def test_local_record_failure_remains_pending_until_authority_query(env, task, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    original = service._record
    try:
        op = proposed(env, task)

        def unavailable(*args, **kwargs):
            raise RuntimeError("PRIVATE_DATABASE_ERROR")

        monkeypatch.setattr(service, "_record", unavailable)
        with pytest.raises(RuntimeError):
            service.execute("t1", op.id)
        assert service.execute("t1", op.id).status == "PENDING"
        monkeypatch.setattr(service, "_record", original)
        assert service.reconcile("t1", op.id).status == "SUCCEEDED"
        _, attrs = spans(service, requests, "operation.execute")[0]
        assert attrs[PREFIX + "outcome"] == "error"
        assert attrs[PREFIX + "status"] == "PENDING"
        assert len(spans(service, requests, "operation.execute")) == 1
        assert service.remote.snapshot("t1", op.resource)["effect_count"] == 1
        assert b"PRIVATE" not in b"".join(r.content for r in requests)
    finally:
        service.telemetry.close()
