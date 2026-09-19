from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from test_collection import source

from agent_py.db import DependencyCircuit, DependencyReadLease, now
from agent_py.domain import DomainError
from agent_py.operations import prometheus_snapshot
from agent_py.resilience import (
    TransientReadError,
    acquire,
    complete,
    dependency_key,
    dependency_status,
    read_with_policy,
    reset_circuit,
    set_read_limit,
    validate_permit,
)
from agent_py.service import Service


def active(service, tenant="t1", key="k"):
    return next(
        r["active_reads"] for r in dependency_status(service, tenant) if r["dependency"] == key
    )


def expire(service, permit):
    with service.db.session(permit.tenant) as s:
        s.execute(
            update(DependencyReadLease)
            .where(DependencyReadLease.id == permit.lease_id)
            .values(expires_at=now() - timedelta(seconds=1))
        )


def test_shared_capacity_concurrency_restart_and_isolation(env):
    service = env[0]
    service.settings.max_reads_per_dependency = 2
    restarted = Service(service.db, service.settings, service.remote)

    def claim(i):
        try:
            return acquire(service if i % 2 else restarted, "t1", "k", "jenkins_build")
        except DomainError as exc:
            assert exc.code == "DEPENDENCY_CAPACITY"

    with ThreadPoolExecutor(max_workers=8) as pool:
        permits = [p for p in pool.map(claim, range(8)) if p]
    assert len(permits) == active(service) == 2
    assert acquire(service, "t2", "k", "jenkins_build")
    assert acquire(service, "t1", "other", "jenkins_build")
    with service.db.session("t1") as s:
        row = s.scalar(
            select(DependencyCircuit).where(
                DependencyCircuit.tenant_id == "t1", DependencyCircuit.dependency == "k"
            )
        )
        assert row.failures == 0 and row.state == "CLOSED"
    complete(restarted, permits[0], "success")
    assert acquire(service, "t1", "k", "jenkins_build")


def test_expiry_reclaims_crashed_owner_and_late_completion_cannot_release_replacement(env):
    service = env[0]
    service.settings.max_reads_per_dependency = 1
    old = acquire(service, "t1", "k", "jenkins_build")
    expire(service, old)
    with pytest.raises(DomainError) as error:
        validate_permit(service, old)
    assert error.value.code == "DEPENDENCY_LEASE_LOST"
    replacement = acquire(service, "t1", "k", "jenkins_build")
    complete(service, old, "failure", 120)
    assert active(service) == 1
    validate_permit(service, replacement)
    assert dependency_status(service, "t1")[0]["failures"] == 0
    complete(service, replacement, "success")
    assert active(service) == 0


def test_duplicate_completion_cannot_double_count_failure(env):
    service = env[0]
    permit = acquire(service, "t1", "k", "jenkins_build")
    complete(service, permit, "failure")
    complete(service, permit, "failure")
    assert dependency_status(service, "t1")[0]["failures"] == 1


def test_lowering_limit_and_config_changes_do_not_evict_or_reinitialize(env):
    service = env[0]
    service.settings.max_reads_per_dependency = 2
    first, second = [acquire(service, "t1", "k", "jenkins_build") for _ in range(2)]
    service.settings.max_reads_per_dependency = 128
    set_read_limit(service, "t1", "k", 1)
    validate_permit(service, first)
    validate_permit(service, second)
    for permit in (first, second):
        with pytest.raises(DomainError, match="capacity"):
            acquire(service, "t1", "k", "jenkins_build")
        complete(service, permit, "success")
    assert dependency_status(service, "t1")[0]["read_limit"] == 1
    assert acquire(service, "t1", "k", "jenkins_build")


@pytest.mark.parametrize("limit", [0, 129, True, 1.5])
def test_invalid_limits_rejected(env, limit):
    with pytest.raises(DomainError) as error:
        set_read_limit(env[0], "t1", "k", limit)
    assert error.value.code == "INVALID_LIMIT"


def test_limit_requires_existing_tenant_dependency(env):
    acquire(env[0], "t1", "k", "jenkins_build")
    with pytest.raises(DomainError) as error:
        set_read_limit(env[0], "t2", "k", 2)
    assert error.value.code == "NOT_FOUND"


def test_reset_fences_results_but_does_not_overbook_active_reads(env):
    service = env[0]
    service.settings.max_reads_per_dependency = 1
    old = acquire(service, "t1", "k", "jenkins_build")
    reset_circuit(service, "t1", "k")
    with pytest.raises(DomainError, match="capacity"):
        acquire(service, "t1", "k", "jenkins_build")
    complete(service, old, "failure", 120)
    assert acquire(service, "t1", "k", "jenkins_build")
    assert dependency_status(service, "t1")[0]["state"] == "CLOSED"


def test_capacity_denial_does_not_claim_half_open_probe(env):
    service = env[0]
    service.settings.max_reads_per_dependency = 1
    owner = acquire(service, "t1", "k", "jenkins_build")
    with service.db.session("t1") as s:
        row = s.scalar(select(DependencyCircuit))
        row.state = "OPEN"
        row.retry_at = now() - timedelta(seconds=1)
        row.generation += 1
    with pytest.raises(DomainError, match="capacity"):
        acquire(service, "t1", "k", "jenkins_build")
    with service.db.session("t1") as s:
        row = s.scalar(select(DependencyCircuit))
        assert row.state == "OPEN" and row.probe_token is None
    complete(service, owner, None)
    assert acquire(service, "t1", "k", "jenkins_build").probe_token


def test_capacity_refusal_makes_no_http_call_and_does_not_sleep(env):
    service, p, _ = env
    service.settings.max_reads_per_dependency = 1
    acquire(service, "t1", dependency_key(source()), "jenkins_build")
    with pytest.raises(DomainError, match="capacity"):
        read_with_policy(
            service,
            p,
            source(),
            lambda: pytest.fail("HTTP invoked"),
            lambda: None,
            sleep=lambda _: pytest.fail("slept"),
        )


@pytest.mark.parametrize("failure", ["permanent", "unexpected", "revoked"])
def test_all_failure_exits_release_their_own_slot(env, failure):
    service, p, _ = env
    checks = []

    def authorize():
        checks.append(1)
        if failure == "revoked" and len(checks) == 2:
            raise DomainError("PERMISSION_REVOKED", "revoked")

    def read():
        if failure == "unexpected":
            raise RuntimeError("unexpected")
        raise DomainError("UPSTREAM_HTTP", "permanent")

    with pytest.raises((DomainError, RuntimeError)):
        read_with_policy(service, p, source(), read, authorize)
    assert active(service, key=dependency_key(source())) == 0


def test_retries_keep_one_slot_and_reject_late_response(env):
    service, p, _ = env
    calls = []

    def read():
        calls.append(1)
        assert active(service, key=dependency_key(source())) == 1
        if len(calls) < 3:
            raise TransientReadError()
        with service.db.session("t1") as s:
            s.execute(update(DependencyReadLease).values(expires_at=now() - timedelta(seconds=1)))
        return {"must_not_publish": True}

    with pytest.raises(DomainError) as error:
        read_with_policy(service, p, source(), read, lambda: None, sleep=lambda _: None)
    assert error.value.code == "DEPENDENCY_LEASE_LOST" and len(calls) == 3
    assert active(service, key=dependency_key(source())) == 0
    assert dependency_status(service, "t1")[0]["failures"] == 0


def test_monitoring_limits_scope_and_labels(env):
    service = env[0]
    service.settings.monitoring_tenants = ["t1"]
    acquire(service, "t1", "private-origin-hash", "jenkins_build")
    acquire(service, "t2", "other-secret-hash", "jenkins_build")
    metrics = prometheus_snapshot(service).decode()
    assert (
        'agent_dependency_read_slots{provider="jenkins_build",state="active",tenant="t1"} 1.0'
        in metrics
    )
    assert (
        'agent_dependency_read_slots{provider="jenkins_build",state="limit",tenant="t1"} 4.0'
        in metrics
    )
    assert 'tenant="t2"' not in metrics and "private-origin-hash" not in metrics


def test_operator_cli_updates_shared_limit_without_releasing_reads(env, monkeypatch):
    import json

    from typer.testing import CliRunner

    from agent_py.cli import app

    service = env[0]
    permit = acquire(service, "t1", "k", "jenkins_build")
    monkeypatch.setattr("agent_py.cli.build_service", lambda *_: service)
    runner = CliRunner()
    assert runner.invoke(app, ["dependency-limit", "t1", "k", "2"]).exit_code == 0
    result = runner.invoke(app, ["dependency-status", "t1"])
    assert result.exit_code == 0
    row = json.loads(result.stdout)[0]
    assert row["read_limit"] == 2 and row["active_reads"] == 1
    validate_permit(service, permit)
