from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest
from sqlalchemy import select
from test_collection import collector, source

from agent_py.adapters.enterprise import EnterpriseClient, retry_delay
from agent_py.db import DependencyCircuit, now
from agent_py.domain import DomainError
from agent_py.resilience import (
    TransientReadError,
    acquire,
    complete,
    dependency_key,
    read_with_policy,
)
from agent_py.service import Service


def fail_closed(service, tenant="t1", key="k"):
    for _ in range(3):
        permit = acquire(service, tenant, key, "jenkins_build")
        complete(service, permit, "failure")


def expire(service, tenant="t1", key="k"):
    with service.db.session(tenant) as s:
        s.scalar(select(DependencyCircuit).where(DependencyCircuit.dependency == key)).retry_at = (
            now() - timedelta(seconds=1)
        )


def test_shared_circuit_survives_service_restart_and_isolates_tenants(env):
    service = env[0]
    fail_closed(service)
    restarted = Service(service.db, service.settings, service.remote)
    with pytest.raises(DomainError, match="cooling"):
        acquire(restarted, "t1", "k", "jenkins_build")
    assert acquire(restarted, "t2", "k", "jenkins_build")
    assert acquire(restarted, "t1", "other-origin", "jenkins_build")


def test_only_one_concurrent_half_open_probe_and_success_closes(env):
    service = env[0]
    fail_closed(service)
    expire(service)

    def attempt(_):
        try:
            return acquire(service, "t1", "k", "jenkins_build")
        except DomainError as exc:
            assert exc.code == "DEPENDENCY_OPEN"

    with ThreadPoolExecutor(max_workers=8) as pool:
        permits = [p for p in pool.map(attempt, range(8)) if p]
    assert len(permits) == 1 and permits[0].probe_token
    complete(service, permits[0], "success")
    assert acquire(service, "t1", "k", "jenkins_build").probe_token is None


def test_late_success_and_expired_probe_cannot_close_new_generation(env):
    service = env[0]
    old = acquire(service, "t1", "k", "jenkins_build")
    fail_closed(service)
    complete(service, old, "success")
    with pytest.raises(DomainError):
        acquire(service, "t1", "k", "jenkins_build")
    expire(service)
    first = acquire(service, "t1", "k", "jenkins_build")
    expire(service)
    replacement = acquire(service, "t1", "k", "jenkins_build")
    complete(service, first, "success")
    with pytest.raises(DomainError):
        acquire(service, "t1", "k", "jenkins_build")
    complete(service, replacement, "failure")
    with pytest.raises(DomainError):
        acquire(service, "t1", "k", "jenkins_build")


def test_bounded_retry_and_success_reset(env):
    service, p, _ = env
    attempts, checks, delays = [], [], []

    def read():
        attempts.append(1)
        if len(attempts) < 3:
            raise TransientReadError()
        return {"ok": True}

    assert read_with_policy(
        service,
        p,
        source(),
        read,
        lambda: checks.append(1),
        sleep=delays.append,
        jitter=lambda a, b: b,
    ) == {"ok": True}
    assert len(attempts) == 3 and len(delays) == 2 and len(checks) == 5
    with service.db.session("t1") as s:
        assert s.scalar(select(DependencyCircuit)).failures == 0


def test_retry_rechecks_cancel_before_another_request(env, task):
    service, p, _ = env
    calls = []

    def authorize():
        with service.db.session("t1") as s:
            service._executable(s, service.get_task(p, task.id))

    def read():
        calls.append(1)
        raise TransientReadError()

    with pytest.raises(DomainError, match="cannot dispatch"):
        read_with_policy(
            service,
            p,
            source(),
            read,
            authorize,
            sleep=lambda _: service.stop(p, task.id),
            jitter=lambda a, b: a,
        )
    assert len(calls) == 1


def test_retry_time_budget_stops_new_attempts(env):
    clock, calls = [0], []

    def read():
        calls.append(1)
        clock[0] = 31
        raise TransientReadError()

    with pytest.raises(TransientReadError):
        read_with_policy(
            env[0],
            env[1],
            source(),
            read,
            lambda: None,
            monotonic=lambda: clock[0],
            sleep=lambda _: pytest.fail("slept"),
        )
    assert len(calls) == 1


def test_rate_limit_is_shared_without_sleep_or_secret_storage(env, task, monkeypatch):
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    reader = collector(env, monkeypatch, response)
    with pytest.raises(TransientReadError):
        reader.collect(env[1], task.id)
    with pytest.raises(DomainError, match="cooling"):
        reader.collect(env[1], task.id)
    assert len(calls) == 1
    with env[0].db.session("t1") as s:
        row = s.scalar(select(DependencyCircuit))
        assert row.state == "OPEN" and row.failures == 1
        assert row.dependency == dependency_key(source())
        assert "private-token" not in str(row.__dict__)


@pytest.mark.parametrize("status", [401, 403, 404, 302])
def test_permanent_http_errors_never_retry(env, task, monkeypatch, status):
    calls = []
    reader = collector(
        env,
        monkeypatch,
        lambda request: (
            calls.append(request)
            or httpx.Response(
                status, text="sensitive provider body", headers={"location": "https://private.test"}
            )
        ),
    )
    with pytest.raises(DomainError) as error:
        reader.collect(env[1], task.id)
    assert len(calls) == 1 and "sensitive" not in str(error.value)
    with env[0].db.session("t1") as s:
        assert s.scalar(select(DependencyCircuit)).failures == 0


def test_collection_retries_transient_get_and_persists_once(env, task, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(503)
            if len(calls) == 1
            else httpx.Response(200, json={"result": "FAILURE"})
        )

    reader = collector(env, monkeypatch, handler)
    assert len(reader.collect(env[1], task.id)) == 1
    assert len(calls) == 2 and all(r.method == "GET" for r in calls)


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b'{"number":NaN}'])
def test_invalid_json_is_permanent_and_does_not_leak_response(body):
    client = EnterpriseClient(
        "https://example.test/",
        "secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    )
    try:
        with pytest.raises(DomainError, match="JSON|object"):
            client.get("resource")
    finally:
        client.close()


def test_retry_after_seconds_date_and_malformed_values():
    assert retry_delay("120") == 120
    assert retry_delay("172800") == 172800
    assert 100 <= retry_delay(format_datetime(now() + timedelta(seconds=120))) <= 120
    assert retry_delay(None) == 30
    assert retry_delay("garbage") == 86400


def test_other_worker_opening_circuit_stops_local_retry(env):
    service, p, _ = env
    calls = []

    def read():
        calls.append(1)
        raise TransientReadError()

    def other_worker(_):
        key = dependency_key(source())
        permit = acquire(service, "t1", key, "jenkins_build")
        complete(service, permit, "failure", 60)

    with pytest.raises(DomainError, match="superseded"):
        read_with_policy(service, p, source(), read, lambda: None, sleep=other_worker)
    assert len(calls) == 1


def test_manual_reset_fences_old_results_and_metrics_are_tenant_scoped(env):
    from agent_py.operations import prometheus_snapshot
    from agent_py.resilience import reset_circuit

    service = env[0]
    old = acquire(service, "t1", "k", "jenkins_build")
    fail_closed(service)
    fail_closed(service, "t2")
    service.settings.monitoring_tenants = ["t1"]
    metrics = prometheus_snapshot(service).decode()
    assert (
        'agent_dependency_circuits{provider="jenkins_build",state="OPEN",tenant="t1"} 1.0'
        in metrics
    )
    assert 'tenant="t2"' not in metrics
    reset_circuit(service, "t1", "k")
    complete(service, old, "failure", 120)
    assert acquire(service, "t1", "k", "jenkins_build").probe_token is None


def test_transport_failures_are_sanitized():
    def fail(_):
        raise httpx.ConnectTimeout("sensitive transport details")

    client = EnterpriseClient(
        "https://example.test/", "secret", transport=httpx.MockTransport(fail)
    )
    try:
        with pytest.raises(TransientReadError) as error:
            client.get("resource")
        assert "sensitive" not in str(error.value)
    finally:
        client.close()


def test_malformed_projected_fields_are_nonretryable():
    from agent_py.collection import project_response

    with pytest.raises(DomainError, match="object fields"):
        project_response("jira_issue", {"fields": []})
