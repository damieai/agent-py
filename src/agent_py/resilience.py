"""Shared circuit state and bounded retries for explicitly read-only evidence collection."""

import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from agent_py.db import DependencyCircuit, now, uid
from agent_py.domain import DomainError, digest


class TransientReadError(DomainError):
    def __init__(self, code="UPSTREAM_UNAVAILABLE", retry_after: float | None = None):
        super().__init__(code, "Enterprise reader temporarily unavailable", 503)
        self.retry_after = retry_after


def dependency_key(source):
    # Credentials themselves never enter keys, logs, or state. Rotation preserves the circuit.
    return digest(
        [source.provider, source.base_url.rstrip("/"), source.token_env, source.username_env]
    )


def lock_circuit(s, tenant, key, provider):
    if s.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    s.execute(
        insert(DependencyCircuit)
        .values(
            id=uid(),
            tenant_id=tenant,
            dependency=key,
            provider=provider,
            created_at=now(),
            revision=0,
            generation=0,
            failures=0,
            state="CLOSED",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "dependency"])
    )
    s.execute(
        update(DependencyCircuit)
        .where(
            DependencyCircuit.tenant_id == tenant,
            DependencyCircuit.dependency == key,
        )
        .values(revision=DependencyCircuit.revision + 1)
    )
    return s.scalar(
        select(DependencyCircuit).where(
            DependencyCircuit.tenant_id == tenant,
            DependencyCircuit.dependency == key,
        )
    )


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


@dataclass(frozen=True)
class Permit:
    tenant: str
    dependency: str
    provider: str
    generation: int
    probe_token: str | None


def acquire(service, tenant, key, provider):
    with service.db.session(tenant) as s:
        row = lock_circuit(s, tenant, key, provider)
        if row.state != "CLOSED":
            if row.retry_at and aware(row.retry_at) > now():
                raise DomainError("DEPENDENCY_OPEN", "Enterprise dependency is cooling down", 503)
            row.state, row.probe_token = "HALF_OPEN", uid()
            row.generation += 1
            row.retry_at = now() + timedelta(seconds=90)
        return Permit(tenant, key, provider, row.generation, row.probe_token)


def complete(service, permit, outcome, retry_after=None):
    with service.db.session(permit.tenant) as s:
        row = lock_circuit(s, permit.tenant, permit.dependency, permit.provider)
        if row.generation != permit.generation or row.probe_token != permit.probe_token:
            return
        probe = row.state == "HALF_OPEN"
        if probe and row.retry_at and aware(row.retry_at) <= now():
            return  # Expired owners cannot close a circuit after their probe lease ends.
        if outcome == "success":
            row.state, row.failures, row.retry_at, row.probe_token = "CLOSED", 0, None, None
            if probe:
                row.generation += 1
        elif outcome == "failure" or probe:
            if outcome == "failure":
                row.failures += 1
            if probe or row.failures >= 3 or retry_after is not None:
                row.state, row.probe_token = "OPEN", None
                row.generation += 1
                try:
                    row.retry_at = now() + timedelta(seconds=max(30, retry_after or 0))
                except OverflowError:
                    row.retry_at = datetime.max.replace(tzinfo=UTC)


def validate_permit(service, permit):
    with service.db.session(permit.tenant) as s:
        row = s.scalar(
            select(DependencyCircuit).where(
                DependencyCircuit.tenant_id == permit.tenant,
                DependencyCircuit.dependency == permit.dependency,
            )
        )
        if (
            not row
            or row.generation != permit.generation
            or row.probe_token != permit.probe_token
            or row.state == "OPEN"
            or (row.state == "HALF_OPEN" and aware(row.retry_at) <= now())
        ):
            raise DomainError(
                "DEPENDENCY_OPEN", "Enterprise read permit expired or was superseded", 503
            )


def reset_circuit(service, tenant, key):
    with service.db.session(tenant) as s:
        existing = s.scalar(
            select(DependencyCircuit).where(
                DependencyCircuit.tenant_id == tenant,
                DependencyCircuit.dependency == key,
            )
        )
        if not existing:
            raise DomainError("NOT_FOUND", "Dependency circuit not found", 404)
        row = lock_circuit(s, tenant, key, existing.provider)
        row.generation += 1
        row.state, row.failures, row.retry_at, row.probe_token = "CLOSED", 0, None, None


def read_with_policy(
    service,
    principal,
    source,
    read,
    authorize,
    *,
    sleep=time.sleep,
    monotonic=time.monotonic,
    jitter=random.uniform,
):
    """The callback is an enterprise GET only; never pass a model or write executor."""
    authorize()
    permit = acquire(service, principal.tenant_id, dependency_key(source), source.provider)
    started = monotonic()
    outcome, retry_after = None, None
    try:
        attempts = 1 if permit.probe_token else 3
        for attempt in range(attempts):
            outcome = None
            authorize()
            validate_permit(service, permit)
            if monotonic() - started >= 30:
                outcome = "failure"
                raise TransientReadError("UPSTREAM_RETRY_BUDGET")
            try:
                result = read()
                authorize()
                outcome = "success"
                service.telemetry.reads.labels(source.provider, "success").inc()
                return result
            except TransientReadError as exc:
                outcome, retry_after = "failure", exc.retry_after
                service.telemetry.reads.labels(source.provider, "transient").inc()
                # Rate-limit feedback is shared immediately; do not hold a Worker while sleeping.
                if retry_after is not None or attempt + 1 == attempts:
                    raise
                delay = jitter(0.1, 0.3 * (2**attempt))
                if monotonic() - started + delay >= 30:
                    raise
                sleep(delay)
    finally:
        complete(service, permit, outcome, retry_after)
