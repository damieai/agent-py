"""Synthetic platform-check protocol and opt-in boundaries, using real OTLP encoding."""

import json

import httpx
import pytest

pytest.importorskip("langfuse")
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from test_langfuse import decode, settings
from typer.testing import CliRunner

from agent_py import cli
from agent_py.langfuse_check import check


def endpoint(mode="healthy"):
    calls, observations = [], []
    reads = []

    def handle(request):
        calls.append(request)
        assert request.headers["Authorization"].startswith("Basic ")
        if request.url.path == "/api/public/projects":
            if mode == "wrong_credentials":
                return httpx.Response(200, json={"data": [{"id": "other", "name": "PRIVATE"}]})
            return httpx.Response(200, json={"data": [{"id": "project", "name": "PRIVATE"}]})
        if request.method == "POST":
            assert request.url.path == "/api/public/otel/v1/traces"
            for span, attrs, _ in decode([request]):
                assert attrs["langfuse.observation.metadata.synthetic"] is True
                observations.append(
                    dict(
                        id=span.span_id.hex(),
                        traceId=span.trace_id.hex(),
                        name=span.name,
                        parentObservationId=span.parent_span_id.hex() or None,
                        type="SPAN",
                        projectId="project",
                        sessionId=attrs["session.id"],
                        environment="test",
                        metadata={"synthetic": True},
                    )
                )
            response = ExportTraceServiceResponse()
            if mode == "partial_ack":
                response.partial_success.rejected_spans = 1
            return httpx.Response(200, content=response.SerializeToString())
        assert request.url.path == "/api/public/v2/observations"
        assert request.url.params["fields"] == "core,basic,metadata"
        assert "fromStartTime" in request.url.params and "toStartTime" in request.url.params
        assert all(row["traceId"] == request.url.params["traceId"] for row in observations)
        reads.append(1)
        if mode in {"unauthorized", "rate_limit", "redirect"}:
            return httpx.Response(
                {"unauthorized": 401, "rate_limit": 429, "redirect": 307}[mode],
                headers={"Location": "https://PRIVATE.invalid"},
                text="PRIVATE",
            )
        if mode == "oversized":
            return httpx.Response(200, content=b"PRIVATE" * 10000)
        if mode == "malformed":
            return httpx.Response(200, text="PRIVATE")
        if mode == "read_failure":
            raise httpx.ConnectError("PRIVATE")
        if mode == "invisible" or (mode == "delayed" and len(reads) == 1):
            return httpx.Response(200, json={"data": [], "meta": {"cursor": None}})
        rows = [dict(row) for row in observations]
        if mode in {"wrong_parent", "wrong_session", "wrong_project"}:
            rows[0][
                {
                    "wrong_parent": "parentObservationId",
                    "wrong_session": "sessionId",
                    "wrong_project": "projectId",
                }[mode]
            ] = "PRIVATE"
        if mode == "duplicate":
            rows.append(rows[0])
        return httpx.Response(200, json={"data": rows, "meta": {"cursor": None}})

    return httpx.MockTransport(handle), calls


def test_default_is_offline_and_requires_full_sampling():
    transport, calls = endpoint()
    report = check(settings(), "project", transport=transport)
    assert report["status"] == "NOT_RUN" and calls == []
    config = settings(langfuse_sample_rate=0.5)
    report = check(config, "project", allow_network=True, transport=transport)
    assert report["errors"] == ["FULL_SAMPLING_REQUIRED"] and calls == []


@pytest.mark.parametrize("mode", ["healthy", "delayed"])
def test_roundtrip_checks_ids_hierarchy_session_and_synthetic_marker(mode):
    transport, calls = endpoint(mode)
    delays = []
    report = check(
        settings(), "project", allow_network=True, transport=transport, sleep=delays.append
    )
    assert report["status"] == "PASS"
    assert report["ingestion_acknowledged"] and report["observations_verified"]
    assert report["read_attempts"] == (2 if mode == "delayed" else 1)
    assert delays == ([2] if mode == "delayed" else [])
    sent = [r for r in calls if r.method == "POST"]
    assert len(sent) == 1 and len(decode(sent)) == 2
    assert "PRIVATE" not in json.dumps(report)
    assert "sk-test" not in json.dumps(report) and "langfuse.example" not in json.dumps(report)


def test_wrong_project_is_rejected_before_any_write():
    transport, calls = endpoint("wrong_credentials")
    report = check(settings(), "project", allow_network=True, transport=transport)
    assert report["errors"] == ["PROJECT_MISMATCH"]
    assert len(calls) == 1 and calls[0].method == "GET"


@pytest.mark.parametrize(
    "mode,code",
    [
        ("partial_ack", "EXPORT_NOT_ACKNOWLEDGED"),
        ("unauthorized", "READ_HTTP_401"),
        ("rate_limit", "READ_HTTP_429"),
        ("redirect", "READ_HTTP_307"),
        ("oversized", "READ_LIMIT"),
        ("malformed", "READ_FAILED"),
        ("read_failure", "READ_FAILED"),
        ("wrong_parent", "OBSERVATION_MISMATCH"),
        ("wrong_session", "OBSERVATION_MISMATCH"),
        ("wrong_project", "OBSERVATION_MISMATCH"),
        ("duplicate", "OBSERVATION_MISMATCH"),
    ],
)
def test_failures_do_not_retry_or_leak_response(mode, code):
    transport, calls = endpoint(mode)
    report = check(
        settings(),
        "project",
        allow_network=True,
        transport=transport,
        sleep=lambda _: pytest.fail("Error must not trigger polling"),
    )
    assert report["status"] == "FAIL" and report["errors"] == [code]
    assert not report["observations_verified"]
    assert len([r for r in calls if r.method == "POST"]) == 1
    assert report["read_attempts"] <= 1
    assert "PRIVATE" not in json.dumps(report)


def test_ingestion_ack_is_not_visibility_acceptance():
    transport, calls = endpoint("invisible")
    report = check(
        settings(),
        "project",
        allow_network=True,
        transport=transport,
        attempts=2,
        sleep=lambda _: None,
    )
    assert report["status"] == "INCOMPLETE"
    assert report["ingestion_acknowledged"] and not report["observations_verified"]
    assert report["errors"] == ["OBSERVATIONS_NOT_VISIBLE"]
    assert report["read_attempts"] == 2
    assert len([r for r in calls if r.method == "POST"]) == 1


def test_cli_default_does_not_construct_business_service(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "get_settings", settings)
    monkeypatch.setattr(cli, "build_service", lambda *_: pytest.fail("No business service"))
    result = CliRunner().invoke(
        cli.app,
        ["langfuse-check", "--expected-project-id", "project", "--output-dir", str(tmp_path)],
    )
    assert result.exit_code == 0 and "NOT_RUN" in result.stdout
    path = next(tmp_path.glob("run-*/report.json"))
    assert path.stat().st_mode & 0o777 == 0o600
    assert not json.loads(path.read_text())["network_requested"]


def test_cli_configuration_error_is_redacted(monkeypatch):
    def bad():
        raise ValueError("PRIVATE_SECRET")

    monkeypatch.setattr(cli, "get_settings", bad)
    result = CliRunner().invoke(cli.app, ["langfuse-check", "--expected-project-id", "project"])
    assert result.exit_code == 2
    assert "PRIVATE" not in result.output


def test_cli_incomplete_roundtrip_exits_nonzero(tmp_path, monkeypatch):
    import agent_py.langfuse_check as module

    transport, calls = endpoint("invisible")
    original = module.check
    monkeypatch.setattr(cli, "get_settings", settings)
    monkeypatch.setattr(cli, "build_service", lambda *_: pytest.fail("No business service"))
    monkeypatch.setattr(module, "check", lambda *a, **kw: original(*a, **kw, transport=transport))
    result = CliRunner().invoke(
        cli.app,
        [
            "langfuse-check",
            "--expected-project-id",
            "project",
            "--allow-network",
            "--attempts",
            "1",
            "--output-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1 and "INCOMPLETE" in result.stdout
    report = json.loads(next(tmp_path.glob("run-*/report.json")).read_text())
    assert report["ingestion_acknowledged"] and not report["observations_verified"]
    assert len([r for r in calls if r.method == "POST"]) == 1
