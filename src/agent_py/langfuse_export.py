"""Opt-in metadata-only Langfuse OTLP adapter; no global instrumentation or business retries."""

import queue
import re
import threading
import time

import httpx
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from agent_py.observation_policy import pseudonym, selected

NAMES = {"task.accepted", "task.dispatch", "worker.tick", "model.generation", "model.result_reused"}
NAMES |= {"diagnostic.probe", "diagnostic.child"}
STAGES = {
    "retrieval.compile": {"strategy": {"lexical", "bm25_rrf"}},
    "tool.read": {
        "provider": {"bitbucket_pr", "jira_issue", "jenkins_build", "kubernetes_deployment"}
    },
    "sandbox.verify": {
        "phase": {"baseline", "candidate"},
        "limit": {"none", "TIMEOUT", "OUTPUT_LIMIT"},
    },
}
STAGE_NUMBERS = {
    "retrieval.compile": {
        "document_count": (0, 100_000),
        "omitted_count": (0, 100_000),
        "context_bytes": (0, 100_000),
    },
    "tool.read": {"attempt": (1, 3)},
    "sandbox.verify": {"exit_code": (-255, 255)},
}
OPERATIONS = {
    "operation.propose",
    "operation.approval",
    "operation.execute",
    "operation.query",
    "operation.result_reused",
    "operation.escalate",
}
for name in OPERATIONS:
    STAGES[name] = {
        "tool": {"create_pr", "trigger_ci", "merge_pr", "deploy", "rollback", "runbook"},
        "status": {"NOT_SUBMITTED", "PENDING", "UNKNOWN", "SUCCEEDED", "FAILED"},
        "execution_mode": {"simulation", "live"},
        "action_kind": {"standard", "rollback"},
    }
    STAGE_NUMBERS[name] = {"attempt": (0, 100)}
STAGES["operation.approval"]["decision"] = {"APPROVED", "REJECTED"}
STAGES["operation.escalate"]["recovery_status"] = {"MANUAL_REVIEW"}
TASK_STAGES = {"task.cancel", "task.takeover", "task.resume", "task.finish"}
for name in TASK_STAGES | {"operation.escalate"}:
    STAGES.setdefault(name, {}).update(
        {
            "task_status": {"QUEUED", "RUNNING", "WAITING", "CANCELLING", "TERMINATED"},
            "task_result": {"SUCCESS", "FAILED", "CANCELLED"},
            "waiting_reason": {
                "HUMAN_TAKEOVER",
                "RECONCILIATION",
                "MANUAL_REVIEW",
                "APPROVAL",
                "HUMAN_REVIEW",
                "EVIDENCE_REQUIRED",
            },
        }
    )
    STAGE_NUMBERS.setdefault(name, {})["version"] = (0, 10**12)
DIGESTS = {"request_digest", "prompt_digest", "context_digest", "release_digest"}
NUMBERS = {"reserved_micro_usd", "input_price", "output_price"}


class LangfuseProcessor(SpanProcessor):
    def __init__(self, settings, counter, transport=None):
        # Import the optional, pinned SDK only when explicitly enabled. This private encoding
        # API is isolated here and covered by wire-format tests; no SDK client is auto-created.
        from langfuse._client.attributes import create_generation_attributes, create_span_attributes
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceResponse,
        )

        self.encode_generation = create_generation_attributes
        self.encode_span = create_span_attributes
        self.encode_spans = encode_spans
        self.response_type = ExportTraceServiceResponse
        self.settings, self.counter = settings, counter
        self.pending = queue.Queue(maxsize=settings.langfuse_queue_size)
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.flushing = threading.Event()
        self.active = False
        self.client = httpx.Client(
            timeout=settings.langfuse_timeout_seconds,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )
        self.thread = threading.Thread(target=self._run, name="agent-langfuse", daemon=True)
        self.thread.start()

    def pseudonym(self, tenant, category, identifier):
        return pseudonym(self.settings, tenant, category, identifier)

    def eligible(self, span):
        attrs = span.attributes or {}
        tenant, task = attrs.get("tenant.id"), attrs.get("task.id")
        return (
            span.name in NAMES | STAGES.keys()
            and tenant == self.settings.langfuse_tenant
            and isinstance(task, str)
            and 0 < len(task) <= 160
            and span.instrumentation_scope is not None
            and span.instrumentation_scope.name == "agent-py"
        )

    def sanitize(self, span):
        if not self.eligible(span):
            return None
        attrs = span.attributes or {}
        tenant, task = attrs["tenant.id"], attrs["task.id"]
        metadata = {"tenant": self.pseudonym(tenant, "tenant", tenant)}
        if span.name in {"diagnostic.probe", "diagnostic.child"}:
            metadata["synthetic"] = True
        if span.name in TASK_STAGES | {"operation.escalate"}:
            for field in ("cancelled", "taken_over", "changed"):
                value = attrs.get("stage." + field)
                if type(value) is bool:
                    metadata[field] = value
        if span.name in OPERATIONS:
            identifier = attrs.get("operation.id")
            if isinstance(identifier, str) and 0 < len(identifier) <= 160:
                metadata["operation_id"] = self.pseudonym(
                    tenant, "operation", task + ":" + identifier
                )
            if span.name in {"operation.propose", "operation.approval"}:
                changed = attrs.get("stage.changed")
                if type(changed) is bool:
                    metadata["changed"] = changed
        if span.name in STAGES:
            for field, allowed in {"outcome": {"completed", "error"}, **STAGES[span.name]}.items():
                value = attrs.get("stage." + field)
                if isinstance(value, str) and value in allowed:
                    metadata[field] = value
            for field, (minimum, maximum) in STAGE_NUMBERS[span.name].items():
                value = attrs.get("stage." + field)
                if type(value) is int and minimum <= value <= maximum:
                    metadata[field] = value
            value = attrs.get("stage.context_digest")
            if (
                span.name == "retrieval.compile"
                and isinstance(value, str)
                and re.fullmatch(r"[a-f0-9]{64}", value)
            ):
                metadata["context_digest"] = value
        for field in DIGESTS:
            value = attrs.get("model." + field)
            if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value):
                metadata[field] = value
        for field in NUMBERS:
            value = attrs.get("model." + field)
            if type(value) is int and 0 <= value <= 10**12:
                metadata[field] = value
        call_key = attrs.get("model.call_key")
        if isinstance(call_key, str) and len(call_key) <= 160:
            metadata["inference_id"] = self.pseudonym(tenant, "inference", task + ":" + call_key)
        outcome = attrs.get("model.outcome")
        if outcome in {"validated", "invalid_response", "response_unknown", "result_reused"}:
            metadata["outcome"] = outcome
        stop = attrs.get("model.stop")
        if type(stop) is bool:
            metadata["stop"] = stop
        workflow = attrs.get("model.workflow")
        if workflow in {"submit_decision", "submit_investigation", "submit_patch"}:
            metadata["workflow"] = workflow
        result = attrs.get("result")
        if result in {"done", "waiting", "progress", "deferred", "error"}:
            metadata["result"] = result
        if span.name == "model.generation":
            model = attrs.get("model.name", "")
            if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,160}", model):
                model = None
            incoming, outgoing = attrs.get("model.input_tokens"), attrs.get("model.output_tokens")
            measured = all(type(v) is int and 0 <= v <= 10**9 for v in (incoming, outgoing))
            metadata["usage_state"] = "provider_reported" if measured else "unknown"
            usage = {"input": incoming, "output": outgoing} if measured else None
            cost = None
            if measured and all(k in metadata for k in ("input_price", "output_price")):
                cost = {
                    "input": incoming * metadata["input_price"] / 1_000_000,
                    "output": outgoing * metadata["output_price"] / 1_000_000,
                }
            encoded = self.encode_generation(
                model=model, metadata=metadata, usage_details=usage, cost_details=cost
            )
        else:
            encoded = self.encode_span(metadata=metadata)
        encoded.update(
            {
                "session.id": self.pseudonym(tenant, "task", task),
                "langfuse.environment": self.settings.environment,
            }
        )
        from agent_py.trace_context import link, pack

        links = []
        if span.name in {"task.dispatch", "worker.tick"}:
            for item in span.links[:3]:
                role = (item.attributes or {}).get("agent.link")
                if safe := link(pack(item.context), role):
                    links.append(safe)
        # Rebuild rather than copy: discard events, unrecognized links, status text, resource attributes,
        # baggage, user input/output and every unrecognized third-party field.
        return ReadableSpan(
            name=span.name,
            context=span.context,
            parent=span.parent,
            links=links,
            resource=Resource({"service.name": "agent-py"}),
            attributes=encoded,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=InstrumentationScope("agent-py-langfuse", "1"),
        )

    def on_end(self, span):
        try:
            if not self.eligible(span):
                self.counter.labels("filtered").inc()
                return
            attrs = span.attributes
            if not selected(self.settings, attrs["tenant.id"], attrs["task.id"]):
                self.counter.labels("sampled_out").inc()
                return
            safe = self.sanitize(span)
            if safe is None:
                self.counter.labels("filtered").inc()
                return
            # Only sanitized protobuf bytes enter the bounded asynchronous queue.
            data = self.encode_spans([safe]).SerializeToString()
            if len(data) > 16384:
                self.counter.labels("dropped").inc()
                return
            with self.lock:
                if self.stopping.is_set():
                    self.counter.labels("dropped").inc()
                    return
                try:
                    self.pending.put_nowait(data)
                    self.counter.labels("queued").inc()
                    self.wake.set()
                except queue.Full:
                    self.counter.labels("dropped").inc()
        except Exception:
            self.counter.labels("sanitization_failed").inc()

    def _run(self):
        try:
            while not self.stopping.is_set() or not self.pending.empty():
                try:
                    data = self.pending.get(timeout=0.05)
                except queue.Empty:
                    continue
                with self.lock:
                    self.active = True
                batch = [data]
                deadline = time.monotonic() + self.settings.langfuse_batch_wait_seconds
                while len(batch) < self.settings.langfuse_batch_size:
                    try:
                        batch.append(self.pending.get_nowait())
                    except queue.Empty:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or self.stopping.is_set() or self.flushing.is_set():
                            break
                        self.wake.wait(remaining)
                        self.wake.clear()
                # Each item is an ExportTraceServiceRequest containing only repeated
                # resource_spans. Concatenation is protobuf merge semantics, preserving
                # every span without decoding payloads or retaining raw application data.
                # At most 64 * 16 KiB can be held by this single exporter thread.
                data = b"".join(batch)
                try:
                    # No redirect, proxy inheritance, retry, or unlimited response download.
                    with self.client.stream(
                        "POST",
                        self.settings.langfuse_base_url.rstrip("/") + "/api/public/otel/v1/traces",
                        content=data,
                        auth=(
                            self.settings.langfuse_public_key.get_secret_value(),
                            self.settings.langfuse_secret_key.get_secret_value(),
                        ),
                        headers={
                            "Content-Type": "application/x-protobuf",
                            "x-langfuse-ingestion-version": "4",
                        },
                    ) as response:
                        if not 200 <= response.status_code < 300:
                            raise RuntimeError("Export rejected")
                        raw = bytearray()
                        for chunk in response.iter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 65536:
                                raise RuntimeError("Export response exceeds limit")
                        acknowledgement = self.response_type.FromString(bytes(raw))
                        if acknowledgement.partial_success.rejected_spans:
                            raise RuntimeError("Export partially rejected")
                    self.counter.labels("exported").inc(len(batch))
                except Exception:
                    # Partial rejection cannot identify individual accepted spans;
                    # conservatively classify the entire batch as failed, never retry.
                    self.counter.labels("export_failed").inc(len(batch))
                finally:
                    with self.lock:
                        self.active = False
                        for _ in batch:
                            self.pending.task_done()
                        if self.pending.unfinished_tasks == 0:
                            self.flushing.clear()
        finally:
            self.client.close()

    def force_flush(self, timeout_millis=3000):
        if self.pending.unfinished_tasks == 0:
            return True
        self.flushing.set()
        self.wake.set()
        deadline = time.monotonic() + timeout_millis / 1000
        while time.monotonic() < deadline:
            if self.pending.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self.pending.unfinished_tasks == 0

    def shutdown(self):
        with self.lock:
            self.stopping.set()
            self.wake.set()
        self.thread.join(timeout=self.settings.langfuse_flush_seconds)
        with self.lock:
            while True:
                try:
                    self.pending.get_nowait()
                    self.pending.task_done()
                    self.counter.labels("dropped").inc()
                except queue.Empty:
                    break
