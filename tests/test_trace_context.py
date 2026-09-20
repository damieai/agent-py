import asyncio
import base64
import json
import os
import subprocess
import sys
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

pytest.importorskip("langfuse")
from test_investigation_loop import reply, setup
from test_langfuse import decode, settings

from agent_py import langfuse_export
from agent_py.api import create_app
from agent_py.db import Outbox, TaskTrace, WorkLease, now
from agent_py.domain import TaskContract
from agent_py.runtime import dispatch_once
from agent_py.scheduling import acquire, execution_lease, release
from agent_py.security import issue_dev_token
from agent_py.telemetry import Telemetry
from agent_py.trace_context import pack, read_links, record_segment, unpack


def configure(env, monkeypatch):
    service = env[0]
    requests, processors = [], []
    original = langfuse_export.LangfuseProcessor

    def factory(config, counter):
        processor = original(
            config,
            counter,
            httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(200)),
        )
        processors.append(processor)
        return processor

    monkeypatch.setattr(langfuse_export, "LangfuseProcessor", factory)
    for key, value in settings().model_dump().items():
        if key.startswith("langfuse_"):
            setattr(service.settings, key, value)
    service.telemetry.close()
    service.telemetry = Telemetry(service.settings)
    return requests, processors


def test_api_origin_atomic_idempotent_and_caller_context_ignored(env, monkeypatch):
    requests, processors = configure(env, monkeypatch)
    service, principal, _ = env
    injected = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    headers = {
        "Authorization": "Bearer " + issue_dev_token(service.settings, principal),
        "Idempotency-Key": "trace-api",
        "traceparent": injected,
        "tracestate": "PRIVATE",
        "baggage": "user.secret=PRIVATE",
    }
    try:
        with TestClient(create_app(service.settings, service.db, service.remote)) as client:
            body = {"kind": "repair", "goal": "Investigate alpha", "project": "demo"}
            first = client.post("/api/v1/tasks", headers=headers, json=body)
            assert first.status_code == 202
            task_id = first.json()["id"]
            with service.db.session("t1") as s:
                origin = s.scalar(select(TaskTrace)).origin
                assert origin != injected and unpack(origin)
                assert s.scalar(select(Outbox)).task_id == task_id
            second = client.post("/api/v1/tasks", headers=headers, json=body)
            assert second.json()["id"] == task_id
            with service.db.session("t1") as s:
                assert s.scalar(select(TaskTrace)).origin == origin
        for processor in processors:
            assert processor.force_flush(1000)
        assert len([span for span, _, _ in decode(requests) if span.name == "task.accepted"]) == 2
        assert b"PRIVATE" not in b"".join(r.content for r in requests)
    finally:
        service.telemetry.close()


def test_dispatch_ack_loss_keeps_original_carrier(env, monkeypatch):
    _, processors = configure(env, monkeypatch)
    service, principal, _ = env
    try:
        task = service.create_task(
            principal,
            TaskContract(kind="repair", goal="Investigate alpha", project="demo"),
            "trace-dispatch",
        )
        client = AsyncMock()
        client.start_workflow.side_effect = ConnectionError("ACK lost")
        with pytest.raises(ConnectionError):
            asyncio.run(dispatch_once(service, client, "t1"))
        first = client.start_workflow.call_args.args[1]
        client.start_workflow.side_effect = None
        asyncio.run(dispatch_once(service, client, "t1"))
        assert client.start_workflow.call_args.args[1] == first
        assert set(first["trace_context"]) == {"traceparent"}
        with service.db.session("t1") as s:
            row = s.scalar(select(TaskTrace))
            assert row.dispatch == first["trace_context"]["traceparent"]
            assert s.scalar(select(Outbox)).delivered
        before = service.get_task(principal, task.id).version
        assert {
            edge.attributes["agent.link"]
            for edge in read_links(service, "t1", task.id, first["trace_context"])
        } == {"origin", "dispatch"}
        assert service.get_task(principal, task.id).version == before
        other = service.create_task(
            principal,
            TaskContract(kind="repair", goal="Another task", project="demo"),
            "trace-other",
        )
        asyncio.run(dispatch_once(service, client, "t1"))
        other_carrier = client.start_workflow.call_args.args[1]["trace_context"]
        assert other.id != task.id
        assert {
            edge.attributes["agent.link"]
            for edge in read_links(service, "t1", task.id, other_carrier)
        } == {"origin"}
    finally:
        service.telemetry.close()


def test_fresh_worker_processes_link_rounds_without_duplicate_inference(env, monkeypatch, tmp_path):
    requests, _ = configure(env, monkeypatch)
    service = env[0]
    _, task = setup(env, lambda _: reply())
    client = AsyncMock()
    try:
        asyncio.run(dispatch_once(service, client, "t1"))
        identity = client.start_workflow.call_args.args[1]
        results = []
        for ordinal in range(3):
            config = tmp_path / f"worker-{ordinal}.json"
            output = tmp_path / f"output-{ordinal}.json"
            config.write_text(
                json.dumps({"root": str(tmp_path), "identity": identity, "output": str(output)})
            )
            child_env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
            child_env["AGENT_TRACE_FIXTURE"] = "1"
            process = subprocess.run(
                [sys.executable, "tests/support/trace_worker.py", str(config)],
                env=child_env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert process.returncode == 0, process.stderr
            item = json.loads(output.read_text())
            results.append(item)
            requests.extend(
                httpx.Request("POST", "https://fixture", content=base64.b64decode(s))
                for s in item["spans"]
            )
        service.telemetry.close()
        assert [item["paid"] for item in results] == [1, 1, 0]
        assert results[0]["result"]["wait"] == "INVESTIGATION_CONTINUE"
        assert results[1]["result"] == results[2]["result"]
        spans = decode(requests)
        roots = [s for s, _, _ in spans if s.name == "worker.tick"]
        assert len(roots) == 3 and len({s.trace_id for s in roots}) == 3
        assert all(not s.parent_span_id for s in roots)
        generations = [s for s, _, _ in spans if s.name == "model.generation"]
        assert len(generations) == 2
        assert all(
            any(g.trace_id == r.trace_id and g.parent_span_id == r.span_id for r in roots)
            for g in generations
        )
        for previous, current in zip(roots, roots[1:]):
            assert any(
                edge.trace_id == previous.trace_id and edge.span_id == previous.span_id
                for edge in current.links
            )
        origin = next(s for s, _, _ in spans if s.name == "task.accepted")
        dispatcher = next(s for s, _, _ in spans if s.name == "task.dispatch")
        assert all(any(edge.span_id == origin.span_id for edge in r.links) for r in roots)
        assert all(any(edge.span_id == dispatcher.span_id for edge in r.links) for r in roots)
        assert len({attrs["session.id"] for _, attrs, _ in spans}) == 1
    finally:
        service.telemetry.close()


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        "",
        "00-" + "0" * 32 + "-" + "a" * 16 + "-01",
        "00-" + "a" * 32 + "-" + "b" * 16 + "-ff",
        "ff-" + "a" * 32 + "-" + "b" * 16 + "-01",
        "x" * 10000,
    ],
)
def test_invalid_context_is_not_propagated(value):
    assert unpack(value) is None


def test_export_links_strip_private_metadata_and_unrecognized_roles(env, monkeypatch):
    from opentelemetry.trace import Link, SpanContext, TraceFlags, TraceState

    requests, processors = configure(env, monkeypatch)
    service = env[0]
    context = SpanContext(123, 456, True, TraceFlags(1), TraceState([("secret", "PRIVATE")]))
    links = [
        Link(context, {"agent.link": "origin", "secret": "PRIVATE"}),
        Link(context, {"agent.link": "PRIVATE"}),
        Link(context, {"secret": "PRIVATE"}),
    ]
    try:
        with service.telemetry.span(
            "worker.tick", links=links, **{"tenant.id": "t1", "task.id": "fixture"}
        ):
            pass
        assert processors[0].force_flush(1000)
        span = decode(requests)[0][0]
        assert len(span.links) == 1
        assert not span.links[0].trace_state
        assert len(span.links[0].attributes) == 1
        assert span.links[0].attributes[0].value.string_value == "origin"
        assert b"PRIVATE" not in b"".join(request.content for request in requests)
    finally:
        service.telemetry.close()


def test_lease_fences_previous_pointer_and_wrong_task_carrier_is_ignored(env, monkeypatch):
    configure(env, monkeypatch)
    service, principal, _ = env
    try:
        task = service.create_task(
            principal,
            TaskContract(kind="repair", goal="Investigate alpha", project="demo"),
            "trace-lease",
        )
        token, _ = acquire(service, "t1", task.id)
        context_token = execution_lease.set(("t1", task.id, token))
        try:
            with service.telemetry.span(
                "worker.tick", new_root=True, **{"tenant.id": "t1", "task.id": task.id}
            ) as first:
                record_segment(service, "t1", task.id, first.get_span_context())
                expected = pack(first.get_span_context())
            with service.db.session("t1") as s:
                s.scalar(select(WorkLease)).expires_at = now()
            newer, _ = acquire(service, "t1", task.id)
            with service.telemetry.span("worker.tick", new_root=True) as stale:
                record_segment(service, "t1", task.id, stale.get_span_context())
            with service.db.session("t1") as s:
                assert s.scalar(select(TaskTrace)).latest == expected
            assert (
                len(
                    read_links(
                        service, "t1", task.id, {"traceparent": expected, "baggage": "SECRET"}
                    )
                )
                == 2
            )
            assert read_links(service, "t2", task.id, {"traceparent": expected}) == []
            release(service, "t1", task.id, newer)
        finally:
            execution_lease.reset(context_token)
    finally:
        service.telemetry.close()


def test_trace_storage_failure_cannot_block_dispatch(env, task, monkeypatch):
    configure(env, monkeypatch)
    import agent_py.trace_context as module

    monkeypatch.setattr(
        module,
        "row_for",
        lambda *args: (_ for _ in ()).throw(RuntimeError("trace store unavailable")),
    )
    client = AsyncMock()
    try:
        asyncio.run(dispatch_once(env[0], client, "t1"))
        assert client.start_workflow.call_args.args[1] == {"tenant": "t1", "task_id": task.id}
        with env[0].db.session("t1") as s:
            assert s.scalar(select(Outbox)).delivered
    finally:
        env[0].telemetry.close()


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("AGENT_TEST_TEMPORAL") != "1", reason="Temporal test server opt-in")
def test_native_temporal_preserves_carrier_and_replays(env, monkeypatch):
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer, Worker

    from agent_py.runtime import Activities, AgentWorkflow

    requests, processors = configure(env, monkeypatch)
    service, principal, _ = env
    task = service.create_task(
        principal,
        TaskContract(kind="repair", goal="Trace native workflow", project="demo"),
        "trace-native",
    )
    service.stop(principal, task.id)

    async def scenario():
        async with await WorkflowEnvironment.start_local() as runtime:
            async with Worker(
                runtime.client,
                task_queue=service.release.task_queue,
                workflows=[AgentWorkflow],
                activities=[Activities(service).tick],
            ):
                await dispatch_once(service, runtime.client, "t1")
                handle = runtime.client.get_workflow_handle(f"agent:t1:{task.id}")
                result = await asyncio.wait_for(handle.result(), 30)
                assert result["done"]
                history = await handle.fetch_history()
                await Replayer(workflows=[AgentWorkflow]).replay_workflow(history)

    try:
        asyncio.run(scenario())
        for processor in processors:
            assert processor.force_flush(1000)
        decoded = decode(requests)
        dispatched = next(span for span, _, _ in decoded if span.name == "task.dispatch")
        tick = next(span for span, _, _ in decoded if span.name == "worker.tick")
        assert not tick.parent_span_id
        assert any(
            edge.span_id == dispatched.span_id and edge.trace_id == dispatched.trace_id
            for edge in tick.links
        )
    finally:
        service.telemetry.close()
