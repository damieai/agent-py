"""Explicit synthetic OTLP round trip; never opens the application DB or a model gateway."""

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version

import httpx

from agent_py.observation_policy import pseudonym


class CheckFailure(Exception):
    pass


def read_json(client, path, params=None):
    try:
        with client.stream("GET", path, params=params) as response:
            if response.status_code != 200:
                raise CheckFailure(f"READ_HTTP_{response.status_code}")
            raw = bytearray()
            deadline = time.monotonic() + 15
            for chunk in response.iter_bytes(chunk_size=8192):
                raw.extend(chunk)
                if len(raw) > 65536 or time.monotonic() > deadline:
                    raise CheckFailure("READ_LIMIT")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise CheckFailure("READ_SCHEMA")
            return result
    except CheckFailure:
        raise
    except Exception:
        raise CheckFailure("READ_FAILED") from None


def check(
    settings,
    expected_project_id,
    *,
    allow_network=False,
    attempts=5,
    transport=None,
    sleep=time.sleep,
):
    report = dict(
        schema_version=1,
        status="NOT_RUN",
        scope="synthetic_platform_roundtrip",
        checked_at=datetime.now(UTC).isoformat(),
        network_requested=allow_network,
        project_verified=False,
        ingestion_acknowledged=False,
        observations_verified=False,
        read_attempts=0,
        endpoint_digest=hashlib.sha256(settings.langfuse_base_url.encode()).hexdigest(),
        project_digest=hashlib.sha256(expected_project_id.encode()).hexdigest(),
        errors=[],
        limitations=[
            "Not a real model, business task, or UI verification",
            "Does not verify membership isolation, retention or deletion",
        ],
    )
    try:
        report["sdk_version"] = version("langfuse")
    except PackageNotFoundError:
        report["errors"].append("SDK_MISSING")
    if not settings.langfuse_enabled:
        report["errors"].append("LANGFUSE_DISABLED")
    if settings.langfuse_sample_rate != 1:
        report["errors"].append("FULL_SAMPLING_REQUIRED")
    if not expected_project_id or len(expected_project_id) > 160:
        report["errors"].append("EXPECTED_PROJECT_REQUIRED")
    if type(attempts) is not int or not 1 <= attempts <= 10:
        report["errors"].append("INVALID_ATTEMPTS")
    if report["errors"]:
        report["status"] = "FAIL"
        return report
    if not allow_network:
        return report

    from opentelemetry.sdk.trace import TracerProvider
    from prometheus_client import CollectorRegistry, Counter

    from agent_py.langfuse_export import LangfuseProcessor

    provider = None
    try:
        with httpx.Client(
            base_url=settings.langfuse_base_url.rstrip("/"),
            auth=(
                settings.langfuse_public_key.get_secret_value(),
                settings.langfuse_secret_key.get_secret_value(),
            ),
            timeout=settings.langfuse_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            projects = read_json(client, "/api/public/projects").get("data")
            if (
                not isinstance(projects, list)
                or len(projects) != 1
                or not isinstance(projects[0], dict)
                or projects[0].get("id") != expected_project_id
            ):
                raise CheckFailure("PROJECT_MISMATCH")
            report["project_verified"] = True
            counter = Counter(
                "diagnostic_export", "Synthetic export", ["result"], registry=CollectorRegistry()
            )
            processor = LangfuseProcessor(settings, counter, transport)
            provider = TracerProvider(shutdown_on_exit=False)
            provider.add_span_processor(processor)
            tracer = provider.get_tracer("agent-py")
            task = "diagnostic-" + uuid.uuid4().hex
            attributes = {"tenant.id": settings.langfuse_tenant, "task.id": task}
            start = datetime.now(UTC) - timedelta(minutes=1)
            # The provider is local, so no business/file exporters or application DB are used.
            from opentelemetry.context import Context

            with tracer.start_as_current_span(
                "diagnostic.probe", attributes=attributes, context=Context()
            ) as root:
                with tracer.start_as_current_span(
                    "diagnostic.child", attributes=attributes
                ) as child:
                    trace_id = format(root.get_span_context().trace_id, "032x")
                    root_id = format(root.get_span_context().span_id, "016x")
                    child_id = format(child.get_span_context().span_id, "016x")
            report.update(trace_id=trace_id, observation_ids=[root_id, child_id])
            flushed = processor.force_flush(int(settings.langfuse_flush_seconds * 1000))
            if not flushed or counter.labels("exported")._value.get() != 2:
                raise CheckFailure("EXPORT_NOT_ACKNOWLEDGED")
            report["ingestion_acknowledged"] = True
            params = dict(
                traceId=trace_id,
                fields="core,basic,metadata",
                limit=10,
                fromStartTime=start.isoformat(),
                toStartTime=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
            )
            expected = {
                root_id: ("diagnostic.probe", None),
                child_id: ("diagnostic.child", root_id),
            }
            session = pseudonym(settings, settings.langfuse_tenant, "task", task)
            for attempt in range(attempts):
                report["read_attempts"] += 1
                body = read_json(client, "/api/public/v2/observations", params)
                rows, meta = body.get("data"), body.get("meta")
                if not isinstance(rows, list) or not isinstance(meta, dict) or meta.get("cursor"):
                    raise CheckFailure("OBSERVATION_SCHEMA")
                seen = set()
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                        raise CheckFailure("OBSERVATION_SCHEMA")
                    identity = row["id"]
                    if identity not in expected or identity in seen:
                        raise CheckFailure("OBSERVATION_MISMATCH")
                    name, parent = expected[identity]
                    if (
                        row.get("traceId") != trace_id
                        or row.get("projectId") != expected_project_id
                        or row.get("name") != name
                        or row.get("type") != "SPAN"
                        or row.get("parentObservationId") != parent
                        or row.get("sessionId") != session
                        or row.get("environment") != settings.environment
                        or not isinstance(row.get("metadata"), dict)
                        or row["metadata"].get("synthetic") is not True
                    ):
                        raise CheckFailure("OBSERVATION_MISMATCH")
                    seen.add(identity)
                if seen == set(expected):
                    report.update(status="PASS", observations_verified=True)
                    return report
                if attempt + 1 < attempts:
                    sleep(2)
            report["status"] = "INCOMPLETE"
            report["errors"].append("OBSERVATIONS_NOT_VISIBLE")
    except CheckFailure as exc:
        report.update(status="FAIL")
        report["errors"].append(str(exc))
    except Exception:
        report.update(status="FAIL")
        report["errors"].append("CHECK_FAILED")
    finally:
        if provider:
            provider.shutdown()
    return report
