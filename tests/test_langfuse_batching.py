"""Batch wire format, per-span accounting and bounded lifecycle behavior."""

import threading
import time

import httpx
import pytest

pytest.importorskip("langfuse")
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from test_langfuse import adapter, decode


def emit(provider, count):
    for i in range(count):
        with provider.get_tracer("agent-py").start_as_current_span(
            "worker.tick", attributes={"tenant.id": "t1", "task.id": f"task-{i}"}
        ):
            pass


@pytest.mark.parametrize("failure", [None, "reject", "partial"])
def test_batch_preserves_spans_and_counts_not_requests(failure):
    requests = []

    def handle(request):
        requests.append(request)
        response = ExportTraceServiceResponse()
        if failure == "partial":
            response.partial_success.rejected_spans = 1
        return httpx.Response(
            503 if failure == "reject" else 200, content=response.SerializeToString()
        )

    provider, processor, counter = adapter(
        handle, langfuse_batch_size=4, langfuse_batch_wait_seconds=1
    )
    try:
        emit(provider, 10)
        assert processor.force_flush(2000)
        assert len(requests) == 3
        decoded = decode(requests)
        assert len(decoded) == len({s.span_id for s, _, _ in decoded}) == 10
        assert len({a["session.id"] for _, a, _ in decoded}) == 10
        assert all(
            len(decode([request])) <= 4 and len(request.content) <= 4 * 16384
            for request in requests
        )
        assert counter.labels("queued")._value.get() == 10
        assert counter.labels("export_failed" if failure else "exported")._value.get() == 10
        assert counter.labels("exported" if failure else "export_failed")._value.get() == 0
    finally:
        provider.shutdown()


@pytest.mark.parametrize("close", [False, True])
def test_flush_and_shutdown_interrupt_batch_wait(close):
    requests = []
    provider, processor, _ = adapter(
        lambda r: requests.append(r) or httpx.Response(200), langfuse_batch_wait_seconds=1
    )
    try:
        emit(provider, 1)
        started = time.monotonic()
        if close:
            provider.shutdown()
        else:
            assert processor.force_flush(500)
        assert time.monotonic() - started < 0.8
        assert len(decode(requests)) == 1
    finally:
        provider.shutdown()


def test_blocked_batch_shutdown_drops_only_queued_spans():
    entered, release = threading.Event(), threading.Event()
    requests = []

    def blocked(request):
        requests.append(request)
        entered.set()
        release.wait(5)
        return httpx.Response(503)

    provider, processor, counter = adapter(
        blocked,
        langfuse_batch_size=4,
        langfuse_queue_size=8,
        langfuse_flush_seconds=0.01,
        langfuse_batch_wait_seconds=1,
    )
    try:
        emit(provider, 4)
        assert entered.wait(2)
        emit(provider, 10)
        assert not processor.force_flush(10)
        started = time.monotonic()
        provider.shutdown()
        assert time.monotonic() - started < 0.5
        assert counter.labels("dropped")._value.get() == 10
        assert processor.pending.qsize() == 0
    finally:
        release.set()
        processor.thread.join(2)
    assert len(requests) == 1
    assert len(decode(requests)) == counter.labels("export_failed")._value.get() == 4
    assert processor.pending.unfinished_tasks == 0


def test_low_volume_batch_sends_without_explicit_flush():
    sent = threading.Event()
    provider, processor, _ = adapter(
        lambda _: sent.set() or httpx.Response(200), langfuse_batch_wait_seconds=0.02
    )
    try:
        emit(provider, 1)
        assert sent.wait(1)
        assert processor.force_flush(1000)
    finally:
        provider.shutdown()


@pytest.mark.parametrize(
    "values",
    [
        {"langfuse_batch_size": 0},
        {"langfuse_batch_size": 65},
        {"langfuse_batch_wait_seconds": -1},
        {"langfuse_batch_wait_seconds": float("nan")},
    ],
)
def test_invalid_batch_limits_rejected(values):
    from pydantic import ValidationError
    from test_langfuse import settings

    with pytest.raises(ValidationError):
        settings(**values)
