"""Real pinned SDK encoding and OTLP protobuf, with an isolated fake HTTP transport."""

import asyncio
import json
import threading
import time

import httpx
import pytest
from pydantic import ValidationError
from test_investigation_loop import reply, setup

pytest.importorskip("langfuse")
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from prometheus_client import CollectorRegistry, Counter

from agent_py.config import Settings
from agent_py.langfuse_export import LangfuseProcessor
from agent_py.telemetry import SafeFileExporter


def settings(**changes):
    return Settings(
        environment="test",
        langfuse_enabled=True,
        langfuse_base_url="https://langfuse.example",
        langfuse_tenant="t1",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        langfuse_pseudonym_key="pseudonym-test-key-" * 3,
        _env_file=None,
        **changes,
    )


def decode(requests):
    result = []
    for request in requests:
        body = ExportTraceServiceRequest.FromString(request.content)
        for resource in body.resource_spans:
            for scope in resource.scope_spans:
                for span in scope.spans:
                    attrs = {
                        a.key: getattr(a.value, a.value.WhichOneof("value"))
                        for a in span.attributes
                    }
                    result.append((span, attrs, resource.resource))
    return result


def adapter(handler, **changes):
    counter = Counter("test_export", "test", ["result"], registry=CollectorRegistry())
    processor = LangfuseProcessor(settings(**changes), counter, httpx.MockTransport(handler))
    provider = TracerProvider(
        shutdown_on_exit=False, resource=Resource({"secret": "RESOURCE_SECRET"})
    )
    provider.add_span_processor(processor)
    return provider, processor, counter


def test_real_sdk_otlp_sanitization_tenant_filter_and_local_export(tmp_path):
    requests = []
    provider, processor, _ = adapter(lambda r: requests.append(r) or httpx.Response(200))
    local = tmp_path / "local.jsonl"
    provider.add_span_processor(SimpleSpanProcessor(SafeFileExporter(local)))
    try:
        tracer = provider.get_tracer("agent-py")
        with tracer.start_as_current_span(
            "worker.tick", attributes={"tenant.id": "t1", "task.id": "task-secret"}
        ):
            with tracer.start_as_current_span(
                "model.generation",
                attributes={
                    "tenant.id": "t1",
                    "task.id": "task-secret",
                    "model.call_key": "paid:1",
                    "model.name": "test-model",
                    "model.outcome": "validated",
                    "model.input_tokens": 3,
                    "model.output_tokens": 2,
                    "model.input_price": 1,
                    "model.output_price": 2,
                    "model.prompt_digest": "a" * 64,
                    "langfuse.observation.input": "PAYLOAD_SECRET",
                    "Authorization": "HEADER_SECRET",
                },
            ) as child:
                child.add_event("EVENT_SECRET", {"exception.message": "EXCEPTION_SECRET"})

            def other_tenant():
                with tracer.start_as_current_span(
                    "model.generation", attributes={"tenant.id": "t2", "task.id": "OTHER_TENANT"}
                ):
                    pass

            asyncio.run(asyncio.to_thread(other_tenant))
        with provider.get_tracer("untrusted").start_as_current_span(
            "model.generation", attributes={"tenant.id": "t1", "task.id": "task-secret"}
        ):
            pass
        assert processor.force_flush(2000)
        spans = decode(requests)
        assert len(spans) == 2
        generation = next(item for item in spans if item[0].name == "model.generation")
        root = next(item for item in spans if item[0].name == "worker.tick")
        assert generation[0].trace_id == root[0].trace_id
        assert generation[0].parent_span_id == root[0].span_id
        assert generation[1]["session.id"] == root[1]["session.id"]
        assert json.loads(generation[1]["langfuse.observation.usage_details"]) == {
            "input": 3,
            "output": 2,
        }
        assert json.loads(generation[1]["langfuse.observation.cost_details"]) == {
            "input": 0.000003,
            "output": 0.000004,
        }
        encoded = b"".join(r.content for r in requests)
        for secret in (
            b"task-secret",
            b"PAYLOAD_SECRET",
            b"HEADER_SECRET",
            b"RESOURCE_SECRET",
            b"EVENT_SECRET",
            b"EXCEPTION_SECRET",
            b"OTHER_TENANT",
            b"paid:1",
        ):
            assert secret not in encoded
        assert not generation[0].events and not generation[0].links
        assert requests[0].url.path == "/api/public/otel/v1/traces"
        assert requests[0].headers["Authorization"].startswith("Basic ")
        assert processor.pseudonym("t1", "task", "x") != processor.pseudonym("t2", "task", "x")
        assert "PAYLOAD_SECRET" not in local.read_text()
    finally:
        provider.shutdown()


@pytest.mark.parametrize("mode", ["success", "lost", "invalid"])
def test_paid_calls_reuse_and_usage_semantics(env, mode):
    requests, paid = [], []

    def model(r):
        paid.append(r)
        if mode == "lost":
            raise httpx.ReadTimeout("PROVIDER_SECRET")
        return reply("secret" if mode == "invalid" else "alpha")

    harness, task = setup(env, model)
    provider, processor, _ = adapter(lambda r: requests.append(r) or httpx.Response(200))
    # Share the application's provider exactly as in production, including to_thread propagation.
    env[0].telemetry.provider.add_span_processor(processor)
    try:

        async def tick():
            with env[0].telemetry.span("worker.tick", **{"tenant.id": "t1", "task.id": task.id}):
                return await asyncio.to_thread(harness.tick, "t1", task.id)

        if mode == "success":
            asyncio.run(tick())
            asyncio.run(tick())
        else:
            with pytest.raises(Exception):
                asyncio.run(tick())
            with pytest.raises(Exception):
                asyncio.run(tick())
        assert processor.force_flush(2000)
        spans = decode(requests)
        generations = [item for item in spans if item[0].name == "model.generation"]
        assert len(generations) == 1 and len(paid) == 1
        attrs = generations[0][1]
        if mode == "lost":
            assert "langfuse.observation.usage_details" not in attrs
            assert "langfuse.observation.cost_details" not in attrs
            assert attrs["langfuse.observation.metadata.usage_state"] == "unknown"
        else:
            assert attrs["langfuse.observation.metadata.usage_state"] == "provider_reported"
        reused = [item for item in spans if item[0].name == "model.result_reused"]
        assert len(reused) == (1 if mode == "success" else 0)
        if reused:
            assert (
                reused[0][1]["langfuse.observation.metadata.inference_id"]
                == attrs["langfuse.observation.metadata.inference_id"]
            )
            assert "langfuse.observation.cost_details" not in reused[0][1]
    finally:
        env[0].telemetry.close()
        provider.shutdown()


def test_queue_full_bounded_shutdown_and_outage_have_no_business_retry():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked(r):
        calls.append(r)
        entered.set()
        release.wait(5)
        return httpx.Response(503)

    provider, processor, counter = adapter(
        blocked, langfuse_queue_size=1, langfuse_flush_seconds=0.01
    )
    tracer = provider.get_tracer("agent-py")

    def emit():
        with tracer.start_as_current_span(
            "model.result_reused", attributes={"tenant.id": "t1", "task.id": "x"}
        ):
            pass

    try:
        emit()
        assert entered.wait(2)
        for _ in range(5):
            emit()
        assert not processor.force_flush(10)
        start = time.monotonic()
        provider.shutdown()
        assert time.monotonic() - start < 0.5
        assert counter.labels("dropped")._value.get() == 5
    finally:
        release.set()
        processor.thread.join(2)
    assert len(calls) == 1 and counter.labels("export_failed")._value.get() == 1


def test_sanitization_failure_drops_and_does_not_raise(monkeypatch):
    provider, processor, counter = adapter(lambda _: httpx.Response(200))
    monkeypatch.setattr(
        processor, "sanitize", lambda _: (_ for _ in ()).throw(ValueError("SECRET"))
    )
    try:
        with provider.get_tracer("agent-py").start_as_current_span("model.generation"):
            pass
        assert counter.labels("sanitization_failed")._value.get() == 1
    finally:
        provider.shutdown()


@pytest.mark.parametrize(
    "field,value",
    [
        ("langfuse_base_url", "http://external.example"),
        ("langfuse_base_url", "https://user:secret@host"),
        ("langfuse_base_url", "https://host?secret=x"),
        ("langfuse_tenant", ""),
        ("langfuse_secret_key", ""),
        ("langfuse_pseudonym_key", "short"),
    ],
)
def test_configuration_fails_before_export(field, value):
    values = settings().model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


def test_disabled_does_not_initialize_sdk_adapter(monkeypatch):
    from agent_py.telemetry import Telemetry

    monkeypatch.setattr(
        "agent_py.langfuse_export.LangfuseProcessor",
        lambda *a: (_ for _ in ()).throw(AssertionError("unexpected adapter")),
    )
    telemetry = Telemetry(Settings(_env_file=None))
    telemetry.close()


@pytest.mark.parametrize("failure", ["redirect", "partial", "oversized"])
def test_export_ack_failure_is_counted_without_retry(failure):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

    requests = []

    def handler(request):
        requests.append(request)
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://other.example"})
        if failure == "oversized":
            return httpx.Response(200, content=b"x" * 65537)
        response = ExportTraceServiceResponse()
        response.partial_success.rejected_spans = 1
        return httpx.Response(200, content=response.SerializeToString())

    provider, processor, counter = adapter(handler)
    try:
        with provider.get_tracer("agent-py").start_as_current_span(
            "model.result_reused", attributes={"tenant.id": "t1", "task.id": "x"}
        ):
            pass
        assert processor.force_flush(2000)
        assert len(requests) == 1
        assert counter.labels("export_failed")._value.get() == 1
    finally:
        provider.shutdown()


def test_configured_telemetry_owns_adapter_and_closes(monkeypatch):
    from agent_py.telemetry import Telemetry

    created = []

    def factory(config, counter):
        processor = LangfuseProcessor(
            config, counter, httpx.MockTransport(lambda _: httpx.Response(200))
        )
        created.append(processor)
        return processor

    monkeypatch.setattr("agent_py.langfuse_export.LangfuseProcessor", factory)
    telemetry = Telemetry(settings())
    with telemetry.span("worker.tick", **{"tenant.id": "t1", "task.id": "x"}):
        pass
    telemetry.close()
    assert len(created) == 1 and not created[0].thread.is_alive()


def test_cli_tick_flushes_telemetry_on_exit(env, monkeypatch):
    from typer.testing import CliRunner

    from agent_py import cli

    service, principal, _ = env
    closed = []
    monkeypatch.setattr(cli, "runtime_build_service", lambda _: service)
    monkeypatch.setattr(cli, "principal", lambda: principal)
    monkeypatch.setattr(service.telemetry, "close", lambda: closed.append(True))
    result = CliRunner().invoke(
        cli.app, ["investigation-create", "Investigate queue", "--request-key", "lf-cli"]
    )
    # Invalid mode still triggers registered cleanup, even when the command fails.
    assert result.exit_code != 0 and closed == [True]
