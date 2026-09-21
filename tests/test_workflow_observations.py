"""Durable workflow lineage is metadata, never request identity or authorization."""

import asyncio
import json

import pytest
from sqlalchemy import select

pytest.importorskip("langfuse")
from test_investigation_loop import reply, setup
from test_langfuse import decode
from test_repair_workflow import advance_to_patch
from test_repair_workflow import repair_env as repair_env
from test_trace_context import configure

from agent_py.artifacts import ArtifactStore
from agent_py.db import Reservation
from agent_py.domain import DomainError
from agent_py.repair import repair_details
from agent_py.runtime import Activities

PREFIX = "langfuse.observation.metadata."


def exported(service, requests, name):
    service.telemetry.close()
    return [(span, attrs) for span, attrs, _ in decode(requests) if span.name == name]


@pytest.mark.parametrize(
    "query,reason",
    [
        (None, "MODEL_STOP"),
        (" investigate ALPHA ", "REPEATED_QUERY"),
        ("alpha capacity", "NO_PROGRESS"),
        ("secret", "NO_EVIDENCE"),
    ],
)
def test_rounds_stop_reason_and_persisted_summary_reuse(env, monkeypatch, query, reason):
    requests, _ = configure(env, monkeypatch)
    harness, task = setup(env, lambda _: reply(query=query))
    try:
        result = harness.tick("t1", task.id)
        if result.get("wait") == "INVESTIGATION_CONTINUE":
            result = harness.tick("t1", task.id)
        assert harness.tick("t1", task.id) == result
        summaries = exported(env[0], requests, "investigation.summary")
        assert len(summaries) == 2
        assert [a[PREFIX + "changed"] for _, a in summaries] == [True, False]
        assert all(
            a[PREFIX + "stop_reason"] == reason and a[PREFIX + "rounds"] == 1 for _, a in summaries
        )
        for name in ("model.generation", "model.result_reused"):
            observations = exported(env[0], requests, name)
            assert observations
            assert all(a[PREFIX + "round"] == 1 for _, a in observations)
        assert b"Uncertain cause" not in b"".join(r.content for r in requests)
    finally:
        env[0].telemetry.close()


def test_unpersisted_investigation_report_has_no_summary(env, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    harness, task = setup(env, lambda _: reply())
    original = ArtifactStore.put
    monkeypatch.setattr(
        ArtifactStore, "put", lambda *a, **kw: (_ for _ in ()).throw(OSError("PRIVATE"))
    )
    try:
        with pytest.raises(OSError):
            harness.tick("t1", task.id)
        env[0].telemetry.provider.force_flush(2000)
        assert not [s for s, _, _ in decode(requests) if s.name == "investigation.summary"]
        monkeypatch.setattr(ArtifactStore, "put", original)
        harness.tick("t1", task.id)
        assert len(exported(env[0], requests, "model.generation")) == 1
        assert exported(env[0], requests, "investigation.summary")[0][1][PREFIX + "changed"] is True
    finally:
        env[0].telemetry.close()


def test_failed_candidate_and_next_candidate_have_distinct_lineage(repair_env, monkeypatch):
    service, principal, task, harness, behavior, model, _, _ = repair_env
    requests, _ = configure((service, principal), monkeypatch)
    behavior["fail_first"] = True
    activities = Activities(service)
    activities.harness = harness
    try:
        for _ in range(10):
            result = asyncio.run(activities.tick({"tenant": "t1", "task_id": task.id}))
            if result.get("wait") == "CANDIDATE_READY_FOR_REVIEW":
                break
        generations = exported(service, requests, "model.generation")
        results = exported(service, requests, "verification.result")
        assert len(model) == len(generations) == len(results) == 2
        assert [a[PREFIX + "candidate_ordinal"] for _, a in generations] == [1, 2]
        assert len({a[PREFIX + "repair_run_id"] for _, a in generations + results}) == 1
        assert [a[PREFIX + "verification_outcome"] for _, a in results] == [
            "CANDIDATE_FAILED",
            "REGRESSION_FIXED",
        ]
        for ordinal in (1, 2):
            observations = [
                a
                for _, a in generations + results + exported(service, requests, "sandbox.verify")
                if a[PREFIX + "candidate_ordinal"] == ordinal
            ]
            assert len(observations) == 4
            assert len({a[PREFIX + "candidate_id"] for a in observations}) == 1
        assert (
            generations[0][1][PREFIX + "candidate_id"] != generations[1][1][PREFIX + "candidate_id"]
        )
        ticks = exported(service, requests, "worker.tick")
        assert "CANDIDATE_FAILED" in [a.get(PREFIX + "phase") for _, a in ticks]
        assert ticks[-1][1][PREFIX + "waiting_reason"] == "CANDIDATE_READY_FOR_REVIEW"
        details = repair_details(service, principal, task.id)
        wire = b"".join(r.content for r in requests)
        assert details["id"].encode() not in wire and b"bounded fixture output" not in wire
        assert all(a["id"].encode() not in wire for a in details["attempts"])
    finally:
        service.telemetry.close()


def test_verification_retry_retains_candidate_and_changes_verification_identity(
    repair_env, monkeypatch
):
    service, principal, task, harness, behavior, model, _, _ = repair_env
    requests, _ = configure((service, principal), monkeypatch)
    try:
        attempt = advance_to_patch(repair_env)
        behavior["sandbox_crash"] = True
        activities = Activities(service)
        activities.harness = harness
        with pytest.raises(OSError):
            asyncio.run(activities.tick({"tenant": "t1", "task_id": task.id}))
        harness.retry_verification(principal, task.id, attempt["id"])
        behavior["sandbox_crash"] = False
        asyncio.run(activities.tick({"tenant": "t1", "task_id": task.id}))
        verification = exported(service, requests, "sandbox.verify")
        assert len(model) == 1 and len(verification) == 3
        assert len({a[PREFIX + "candidate_id"] for _, a in verification}) == 1
        assert len({a[PREFIX + "verification_id"] for _, a in verification}) == 2
        assert verification[0][1][PREFIX + "outcome"] == "error"
        result = exported(service, requests, "verification.result")
        assert len(result) == 1
        assert (
            result[0][1][PREFIX + "verification_id"]
            == verification[-1][1][PREFIX + "verification_id"]
        )
        assert exported(service, requests, "worker.tick")[0][1][PREFIX + "result"] == "error"
    finally:
        service.telemetry.close()


def test_observation_lineage_does_not_change_paid_request_or_reuse_identity(
    repair_env, monkeypatch
):
    service, principal, task, harness, _, model, _, _ = repair_env
    requests, _ = configure((service, principal), monkeypatch)
    try:
        advance_to_patch(repair_env)
        payload = json.loads(model[0].content)
        message = json.loads(payload["messages"][0]["content"])
        details = repair_details(service, principal, task.id)
        with service.db.session("t1") as s:
            reservation = s.scalar(select(Reservation))
        # Existing callers and previously persisted request hashes remain valid without lineage.
        harness.gateway.propose_patch(
            "t1", task.id, reservation.call_key, message["goal"], message["context"]
        )
        harness.gateway.propose_patch(
            "t1",
            task.id,
            reservation.call_key,
            message["goal"],
            message["context"],
            repair_run_id=details["id"],
            candidate_ordinal=1,
        )
        assert len(model) == 1
        generation = exported(service, requests, "model.generation")[0][1]
        reused = exported(service, requests, "model.result_reused")
        assert len(reused) == 2
        assert all(
            a[PREFIX + "request_digest"] == generation[PREFIX + "request_digest"] for _, a in reused
        )
        assert reused[-1][1][PREFIX + "candidate_id"] == generation[PREFIX + "candidate_id"]
        assert "langfuse.observation.cost_details" not in reused[-1][1]
    finally:
        service.telemetry.close()


@pytest.mark.parametrize(
    "code,reason",
    [
        ("PRIVATE_CODE", "OTHER"),
        ("BUDGET_EXHAUSTED", "BUDGET_EXHAUSTED"),
        ("PERMISSION_REVOKED", "PERMISSION_REVOKED"),
    ],
)
def test_worker_blocking_reason_is_bounded_and_does_not_leak(env, task, monkeypatch, code, reason):
    requests, _ = configure(env, monkeypatch)
    activities = Activities(env[0])

    def blocked(*args):
        raise DomainError(code, "PRIVATE_MESSAGE")

    monkeypatch.setattr(activities.harness, "tick", blocked)
    try:
        result = asyncio.run(activities.tick({"tenant": "t1", "task_id": task.id}))
        assert result["blocked"] == code
        attrs = exported(env[0], requests, "worker.tick")[0][1]
        assert attrs[PREFIX + "waiting_reason"] == reason and attrs[PREFIX + "blocked"] is True
        assert b"PRIVATE" not in b"".join(r.content for r in requests)
    finally:
        env[0].telemetry.close()


def test_verification_artifact_failure_does_not_emit_a_persisted_result(repair_env, monkeypatch):
    service, principal, task, harness, _, _, _, _ = repair_env
    requests, _ = configure((service, principal), monkeypatch)
    advance_to_patch(repair_env)
    original = ArtifactStore.put

    def fail(self, tenant, task_id, kind, *args, **kwargs):
        if kind == "candidate-verification":
            raise OSError("PRIVATE_STORAGE_ERROR")
        return original(self, tenant, task_id, kind, *args, **kwargs)

    monkeypatch.setattr(ArtifactStore, "put", fail)
    try:
        with pytest.raises(OSError):
            harness.tick("t1", task.id)
        assert len(exported(service, requests, "sandbox.verify")) == 2
        assert exported(service, requests, "verification.result") == []
        assert b"PRIVATE_STORAGE_ERROR" not in b"".join(r.content for r in requests)
    finally:
        service.telemetry.close()


def test_lineage_is_scoped_and_invalid_fields_never_reach_wire(env, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    try:
        for task_id in ("PRIVATE_TASK_A", "PRIVATE_TASK_B"):
            with service.telemetry.span(
                "sandbox.verify",
                **{
                    "tenant.id": "t1",
                    "task.id": task_id,
                    "repair.run_id": "PRIVATE_RUN",
                    "repair.candidate_ordinal": 1,
                    "verification.id": "PRIVATE_TOKEN",
                },
            ):
                pass
        with service.telemetry.span(
            "model.generation",
            **{
                "tenant.id": "t1",
                "task.id": "PRIVATE_TASK_A",
                "model.workflow": "submit_investigation",
                "investigation.round": True,
                "repair.run_id": "x" * 161,
                "repair.candidate_ordinal": 4,
            },
        ):
            pass
        with service.telemetry.span(
            "verification.result",
            **{
                "tenant.id": "t2",
                "task.id": "PRIVATE_OTHER_TENANT",
                "stage.verification_outcome": "REGRESSION_FIXED",
            },
        ):
            pass
        pairs = exported(service, requests, "sandbox.verify")
        for key in ("candidate_id", "repair_run_id", "verification_id"):
            assert pairs[0][1][PREFIX + key] != pairs[1][1][PREFIX + key]
        invalid = exported(service, requests, "model.generation")[0][1]
        assert not any(
            PREFIX + key in invalid
            for key in ("round", "candidate_ordinal", "candidate_id", "repair_run_id")
        )
        assert exported(service, requests, "verification.result") == []
        assert b"PRIVATE" not in b"".join(r.content for r in requests)
    finally:
        service.telemetry.close()


def test_investigation_commit_failure_never_announces_review_transition(env, monkeypatch):
    from contextlib import contextmanager

    from agent_py.db import TaskEvent

    requests, _ = configure(env, monkeypatch)
    harness, task = setup(env, lambda _: reply())
    original = env[0].db.session

    @contextmanager
    def fail_review_commit(tenant=None):
        with original(tenant) as session:
            yield session
            if any(
                isinstance(row, TaskEvent) and row.event_type == "investigation.completed"
                for row in session.new
            ):
                raise OSError("PRIVATE_COMMIT_ERROR")

    monkeypatch.setattr(env[0].db, "session", fail_review_commit)
    try:
        with pytest.raises(OSError):
            harness.tick("t1", task.id)
        env[0].telemetry.provider.force_flush(2000)
        assert not [s for s, _, _ in decode(requests) if s.name == "investigation.summary"]
        monkeypatch.setattr(env[0].db, "session", original)
        assert env[0].get_task(env[1], task.id).waiting_reason != "HUMAN_REVIEW"
        harness.tick("t1", task.id)
        assert len(exported(env[0], requests, "model.generation")) == 1
        assert exported(env[0], requests, "investigation.summary")[0][1][PREFIX + "changed"] is True
    finally:
        env[0].telemetry.close()
