import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from agent_py.api import create_app
from agent_py.db import Grant, Operation, TaskEvent, now
from agent_py.domain import DomainError, TaskContract
from agent_py.metrics_server import start_worker_metrics
from agent_py.operations import summary
from agent_py.runtime import dispatcher_round
from agent_py.security import issue_dev_token
from agent_py.telemetry import SafeFileExporter


def test_operations_summary_respects_projects_and_live_grants(env, task):
    service, p, _ = env
    service.create_task(
        p.model_copy(update={"tenant_id": "t2"}),
        TaskContract(kind="repair", goal="Hidden tenant task", project="demo"),
        "hidden",
    )
    assert summary(service, "t1", p)["tasks"] == {"QUEUED": 1}
    assert summary(service, "t1", p.model_copy(update={"projects": []}))["tasks"] == {}
    with pytest.raises(DomainError, match="operator"):
        summary(service, "t1", p.model_copy(update={"roles": ["developer"]}))
    with service.db.session("t1") as s:
        s.scalar(
            select(Grant).where(Grant.subject == p.subject, Grant.tenant_id == "t1")
        ).revoked = True
    assert summary(service, "t1", p)["tasks"] == {}


def test_metrics_auth_bounded_labels_and_business_age(env, task):
    from test_execution import proposed

    service, p, _ = env
    service.settings.metrics_secret = SecretStr("m" * 40)
    service.settings.monitoring_tenants = ["t1"]
    operation = proposed(env, task)
    with service.db.session("t1") as s:
        op = s.get(Operation, operation.id)
        op.status = "UNKNOWN"
        op.updated_at = now() - timedelta(minutes=20)
    with TestClient(create_app(service.settings, service.db, service.remote)) as client:
        assert client.get("/metrics").status_code == 403
        user_headers = {"Authorization": "Bearer " + issue_dev_token(service.settings, p)}
        assert client.get("/metrics", headers=user_headers).status_code == 403
        assert (
            client.get("/api/v1/ops/summary", headers=user_headers).json()[
                "oldest_unconfirmed_seconds"
            ]
            >= 1200
        )
        for suffix in ("private-one", "private-two"):
            client.get("/not-found/" + suffix)
        response = client.get("/metrics", headers={"Authorization": "Bearer " + "m" * 40})
        assert response.status_code == 200
        assert 'agent_operations{state="UNKNOWN",tenant="t1"} 1.0' in response.text
        assert "private-one" not in response.text and task.id not in response.text
        assert 'route="unmatched"' in response.text


def test_trace_correlation_excludes_payloads_and_headers(env, tmp_path):
    service, p, _ = env
    service.settings.trace_file = tmp_path / "trace.jsonl"
    with TestClient(create_app(service.settings, service.db, service.remote)) as client:
        headers = {
            "Authorization": "Bearer " + issue_dev_token(service.settings, p),
            "Idempotency-Key": "trace-task",
        }
        response = client.post(
            "/api/v1/tasks",
            headers=headers,
            json={"kind": "repair", "project": "demo", "goal": "PRIVATE_GOAL_DO_NOT_EXPORT"},
        )
        assert response.status_code == 202
        trace_id = response.headers["x-trace-id"]
        with service.db.session("t1") as s:
            event = s.scalar(select(TaskEvent).where(TaskEvent.task_id == response.json()["id"]))
            assert event.payload["trace_id"] == trace_id
    raw = "".join(path.read_text() for path in tmp_path.glob("trace.*.jsonl"))
    assert trace_id in raw and "PRIVATE_GOAL_DO_NOT_EXPORT" not in raw
    assert headers["Authorization"] not in raw
    rows = [json.loads(line) for line in raw.splitlines()]
    assert rows[0]["attributes"]["http.route"] == "/api/v1/tasks"


def test_worker_scrape_uses_own_registry_and_auth(env):
    service = env[0]
    service.settings.metrics_secret = SecretStr("m" * 40)
    service.settings.worker_metrics_enabled = True
    service.settings.worker_metrics_port = 0
    service.telemetry.ticks.labels("progress").inc()
    server = start_worker_metrics(service)
    try:
        address = f"http://127.0.0.1:{server.server_address[1]}/metrics"
        assert httpx.get(address).status_code == 403
        response = httpx.get(address, headers={"Authorization": "Bearer " + "m" * 40})
        assert (
            response.status_code == 200
            and 'agent_worker_ticks_total{result="progress"} 1.0' in response.text
        )
        assert "agent_tasks" not in response.text
    finally:
        server.shutdown()
        server.server_close()


def test_dispatch_failure_does_not_skip_other_tenant_or_reconciliation(env, monkeypatch):
    import agent_py.runtime as runtime
    import agent_py.webhooks as webhooks

    called = []

    async def dispatch(service, client, tenant):
        called.append((tenant, "dispatch"))
        if tenant == "t1":
            raise ConnectionError("private provider body")

    monkeypatch.setattr(runtime, "dispatch_once", dispatch)
    monkeypatch.setattr(
        runtime, "reconcile_once", lambda service, tenant: called.append((tenant, "reconcile"))
    )
    monkeypatch.setattr(
        webhooks, "consume", lambda service, tenant: called.append((tenant, "inbox"))
    )
    asyncio.run(dispatcher_round(env[0], AsyncMock(), ["t1", "t2"]))
    assert called == [
        (t, stage) for t in ["t1", "t2"] for stage in ["dispatch", "reconcile", "inbox"]
    ]


def test_rotated_trace_files_are_private(tmp_path):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    exporter = SafeFileExporter(tmp_path / "trace.jsonl")
    exporter.handler.maxBytes = 200
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    for _ in range(4):
        with provider.get_tracer("test").start_as_current_span("test"):
            pass
    provider.shutdown()
    assert len(list(tmp_path.iterdir())) > 1
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.iterdir())


def test_reconciler_rotates_past_persistent_unknown_backlog(env, task, monkeypatch):
    from agent_py.runtime import reconcile_once

    service = env[0]
    with service.db.session("t1") as s:
        for i in range(105):
            s.add(
                Operation(
                    id=f"op-{i:04}",
                    tenant_id="t1",
                    task_id=task.id,
                    step_key=str(i),
                    tool="create_pr",
                    resource="demo-service",
                    parameters={},
                    payload_digest="a" * 64,
                    status="UNKNOWN",
                )
            )
    queried = []
    monkeypatch.setattr(service, "reconcile", lambda tenant, operation: queried.append(operation))
    reconcile_once(service, "t1")
    reconcile_once(service, "t1")
    assert len(queried) == len(set(queried)) == 105
