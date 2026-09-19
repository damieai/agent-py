"""Bounded metric labels and allowlisted OpenTelemetry export; never record payloads."""

import json
import logging
import os
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from prometheus_client import CollectorRegistry, Counter, Histogram


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        descriptor = os.open(
            self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        return os.fdopen(descriptor, "a", encoding="utf-8")


class SafeFileExporter(SpanExporter):
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handler = PrivateRotatingHandler(
            path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        os.chmod(path, 0o600)
        self.handler.setFormatter(logging.Formatter("%(message)s"))

    def export(self, spans):
        for span in spans:
            data = {
                "name": span.name,
                "trace_id": format(span.context.trace_id, "032x"),
                "span_id": format(span.context.span_id, "016x"),
                "duration_ms": (span.end_time - span.start_time) / 1_000_000,
                "attributes": {
                    k: v
                    for k, v in span.attributes.items()
                    if k
                    in {
                        "http.method",
                        "http.route",
                        "http.status_code",
                        "result",
                        "error.type",
                        "task.id",
                        "tenant.id",
                    }
                },
            }
            self.handler.handle(
                logging.LogRecord("agent.trace", logging.INFO, "", 0, json.dumps(data), (), None)
            )
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.handler.close()


class Telemetry:
    def __init__(self, settings):
        self.registry = CollectorRegistry()
        self.http = Histogram(
            "agent_http_duration_seconds",
            "API latency by route template",
            ["method", "route", "status"],
            registry=self.registry,
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 15),
        )
        self.ticks = Counter(
            "agent_worker_ticks_total", "Worker tick results", ["result"], registry=self.registry
        )
        self.reads = Counter(
            "agent_enterprise_reads_total",
            "Enterprise read attempt outcomes",
            ["provider", "result"],
            registry=self.registry,
        )
        self.tick_duration = Histogram(
            "agent_worker_tick_duration_seconds", "Worker tick wall time", registry=self.registry
        )
        self.provider = TracerProvider(shutdown_on_exit=False)
        if settings.trace_file:
            from agent_py.db import uid

            path = settings.trace_file
            path = path.with_name(f"{path.stem}.{os.getpid()}.{uid()[:8]}.jsonl")
            self.provider.add_span_processor(SimpleSpanProcessor(SafeFileExporter(path)))
        self.tracer = self.provider.get_tracer("agent-py", "0.1.0")

    @contextmanager
    def span(self, name, **attributes):
        with self.tracer.start_as_current_span(
            name, attributes=attributes, record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                yield span
            except Exception as exc:
                span.set_attribute("error.type", type(exc).__name__[:80])
                raise

    def close(self):
        self.provider.shutdown()
