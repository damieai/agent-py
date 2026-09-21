"""Stage semantics and privacy checked against real OTLP encoding; no remote services."""

import asyncio

import httpx
import pytest

pytest.importorskip("langfuse")
from test_collection import collector, source
from test_investigation_loop import reply, setup
from test_langfuse import decode
from test_trace_context import configure
from test_verification import prepared

from agent_py.context import ContextCompiler
from agent_py.domain import DomainError
from agent_py.resilience import TransientReadError, read_with_policy
from agent_py.runtime import Activities
from agent_py.sandbox import SandboxResult
from agent_py.verification import VerificationRunner

PREFIX = "langfuse.observation.metadata."


def exported(service, requests, name):
    service.telemetry.close()
    return [(span, attrs) for span, attrs, _ in decode(requests) if span.name == name]


@pytest.mark.parametrize("strategy", ["lexical", "bm25_rrf"])
def test_retrieval_parent_summary_and_persisted_round_reuse(env, monkeypatch, strategy):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    service.settings.context_strategy = strategy
    harness, task = setup(env, lambda _: reply())
    activities = Activities(service)
    activities.harness = harness
    try:
        first = asyncio.run(activities.tick({"tenant": "t1", "task_id": task.id}))
        assert asyncio.run(activities.tick({"tenant": "t1", "task_id": task.id})) == first
        retrievals = exported(service, requests, "retrieval.compile")
        assert len(retrievals) == 1
        span, attrs = retrievals[0]
        assert attrs[PREFIX + "strategy"] == strategy
        assert attrs[PREFIX + "document_count"] >= 1
        assert attrs[PREFIX + "context_bytes"] > 0
        assert len(attrs[PREFIX + "context_digest"]) == 64
        roots = [s for s, _, _ in decode(requests) if s.name == "worker.tick"]
        assert any(span.parent_span_id == r.span_id and span.trace_id == r.trace_id for r in roots)
        wire = b"".join(r.content for r in requests)
        assert b"fixture://" not in wire and b"Investigate alpha" not in wire
    finally:
        service.telemetry.close()


def test_retrieval_denial_has_no_counts_or_exception_text(env, task, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    compiler = ContextCompiler(service.db, telemetry=service.telemetry)
    try:
        with pytest.raises(DomainError):
            compiler.compile(env[1], "PRIVATE_PROJECT", "lab", "PRIVATE_QUERY", task_id=task.id)
        _, attrs = exported(service, requests, "retrieval.compile")[0]
        assert attrs[PREFIX + "outcome"] == "error"
        assert PREFIX + "document_count" not in attrs
        wire = b"".join(r.content for r in requests)
        assert b"PRIVATE" not in wire and b"principal scope" not in wire
    finally:
        service.telemetry.close()


def test_tool_retries_count_actual_callbacks_and_denial_is_not_a_read(env, task, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service, principal, _ = env
    attempts = []

    def read():
        attempts.append(1)
        if len(attempts) == 1:
            raise TransientReadError("PRIVATE_ERROR")
        return {"PRIVATE_RESULT": "secret"}

    try:
        read_with_policy(
            service, principal, source(), read, lambda: None, task_id=task.id, sleep=lambda _: None
        )

        def denied():
            raise DomainError("FORBIDDEN", "PRIVATE_AUTH")

        with pytest.raises(DomainError):
            read_with_policy(service, principal, source(), read, denied, task_id=task.id)
        spans = exported(service, requests, "tool.read")
        assert len(attempts) == len(spans) == 2
        assert [a[PREFIX + "attempt"] for _, a in spans] == [1, 2]
        assert [a[PREFIX + "outcome"] for _, a in spans] == ["error", "completed"]
        assert all(a[PREFIX + "provider"] == "jenkins_build" for _, a in spans)
        wire = b"".join(r.content for r in requests)
        assert b"PRIVATE" not in wire and b"ci.example" not in wire
    finally:
        service.telemetry.close()


def test_collection_passes_task_identity(env, task, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    reader = collector(env, monkeypatch, lambda _: httpx.Response(200, json={"number": 42}))
    try:
        assert reader.collect(env[1], task.id)
        assert len(exported(env[0], requests, "tool.read")) == 1
        assert b"private-token" not in b"".join(r.content for r in requests)
    finally:
        env[0].telemetry.close()


@pytest.mark.parametrize("failure", [False, True])
def test_sandbox_phases_limits_and_error_do_not_export_logs(
    env, task, tmp_path, monkeypatch, failure
):
    requests, _ = configure(env, monkeypatch)
    source_path, edit = prepared(env, tmp_path)
    calls = []

    class Sandbox:
        def __init__(self, *args):
            pass

        def verify(self, workspace, **kwargs):
            calls.append(1)
            if failure:
                raise FileNotFoundError("PRIVATE_DOCKER_ERROR")
            return SandboxResult(1, "PRIVATE_LOG", "TIMEOUT" if len(calls) == 2 else None)

    try:
        runner = VerificationRunner(env[0], Sandbox)
        if failure:
            with pytest.raises(FileNotFoundError):
                runner.run(env[1], task.id, source_path, [edit])
        else:
            assert runner.run(env[1], task.id, source_path, [edit])["outcome"] == "INCONCLUSIVE"
        spans = exported(env[0], requests, "sandbox.verify")
        assert [a[PREFIX + "phase"] for _, a in spans] == (
            ["baseline"] if failure else ["baseline", "candidate"]
        )
        if failure:
            assert spans[0][1][PREFIX + "outcome"] == "error"
            assert PREFIX + "exit_code" not in spans[0][1]
        else:
            assert spans[1][1][PREFIX + "limit"] == "TIMEOUT"
        wire = b"".join(r.content for r in requests)
        assert b"PRIVATE" not in wire and str(tmp_path).encode() not in wire
    finally:
        env[0].telemetry.close()


def test_stage_metadata_is_allowlisted_typed_and_tenant_filtered(env, monkeypatch):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    try:
        for tenant in ("t1", "t2"):
            with service.telemetry.span(
                "retrieval.compile",
                **{
                    "tenant.id": tenant,
                    "task.id": "private-task",
                    "stage.strategy": "PRIVATE_STRATEGY",
                    "stage.document_count": True,
                    "stage.omitted_count": -1,
                    "stage.context_bytes": 100_001,
                    "stage.context_digest": "PRIVATE_BODY",
                    "stage.output": "PRIVATE_OUTPUT",
                    "stage.outcome": "completed",
                },
            ):
                pass
        spans = exported(service, requests, "retrieval.compile")
        assert len(spans) == 1
        metadata = {k.removeprefix(PREFIX) for k in spans[0][1] if k.startswith(PREFIX)}
        assert metadata == {"tenant", "outcome"}
        assert b"PRIVATE" not in b"".join(r.content for r in requests)
    finally:
        service.telemetry.close()
