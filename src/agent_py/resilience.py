"""Shared circuit state and bounded retries for explicitly read-only evidence collection."""

import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update

from agent_py.db import DependencyCircuit, DependencyReadLease, now, uid
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
    lease_id: str


def acquire(service, tenant, key, provider):
    with service.db.session(tenant) as s:
        row = lock_circuit(s, tenant, key, provider)
        when = now()
        if row.state != "CLOSED" and row.retry_at and aware(row.retry_at) > when:
            raise DomainError("DEPENDENCY_OPEN", "Enterprise dependency is cooling down", 503)
        if row.max_in_flight == 0:
            row.max_in_flight = service.settings.max_reads_per_dependency
        s.execute(
            delete(DependencyReadLease).where(
                DependencyReadLease.tenant_id == tenant,
                DependencyReadLease.dependency == key,
                DependencyReadLease.expires_at <= when,
            )
        )
        active = s.scalar(
            select(func.count())
            .select_from(DependencyReadLease)
            .where(
                DependencyReadLease.tenant_id == tenant,
                DependencyReadLease.dependency == key,
                DependencyReadLease.expires_at > when,
            )
        )
        if active >= row.max_in_flight:
            service.telemetry.reads.labels(provider, "capacity").inc()
            raise DomainError(
                "DEPENDENCY_CAPACITY", "Enterprise dependency read capacity is full", 503
            )
        if row.state != "CLOSED":
            row.state, row.probe_token = "HALF_OPEN", uid()
            row.generation += 1
            row.retry_at = when + timedelta(seconds=90)
        lease_id = uid()
        s.add(
            DependencyReadLease(
                id=lease_id,
                tenant_id=tenant,
                dependency=key,
                expires_at=when + timedelta(seconds=90),
            )
        )
        return Permit(tenant, key, provider, row.generation, row.probe_token, lease_id)


def complete(service, permit, outcome, retry_after=None):
    with service.db.session(permit.tenant) as s:
        row = lock_circuit(s, permit.tenant, permit.dependency, permit.provider)
        expiry = s.scalar(
            delete(DependencyReadLease)
            .where(
                DependencyReadLease.id == permit.lease_id,
                DependencyReadLease.tenant_id == permit.tenant,
                DependencyReadLease.dependency == permit.dependency,
            )
            .returning(DependencyReadLease.expires_at)
        )
        if expiry is None or aware(expiry) <= now():
            return  # Duplicate or expired owners must not alter breaker state.
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
        if not s.scalar(
            select(DependencyReadLease.id).where(
                DependencyReadLease.id == permit.lease_id,
                DependencyReadLease.tenant_id == permit.tenant,
                DependencyReadLease.dependency == permit.dependency,
                DependencyReadLease.expires_at > now(),
            )
        ):
            raise DomainError(
                "DEPENDENCY_LEASE_LOST", "Enterprise read lease expired or was released", 503
            )
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


def set_read_limit(service, tenant, key, limit):
    if type(limit) is not int or not 1 <= limit <= 128:
        raise DomainError("INVALID_LIMIT", "Dependency read limit must be between 1 and 128", 422)
    with service.db.session(tenant) as s:
        existing = s.scalar(
            select(DependencyCircuit).where(
                DependencyCircuit.tenant_id == tenant,
                DependencyCircuit.dependency == key,
            )
        )
        if not existing:
            raise DomainError("NOT_FOUND", "Dependency circuit not found", 404)
        lock_circuit(s, tenant, key, existing.provider).max_in_flight = limit


def dependency_status(service, tenant):
    with service.db.session(tenant) as s:
        active = dict(
            s.execute(
                select(DependencyReadLease.dependency, func.count())
                .where(
                    DependencyReadLease.tenant_id == tenant,
                    DependencyReadLease.expires_at > now(),
                )
                .group_by(DependencyReadLease.dependency)
            ).all()
        )
        rows = s.scalars(
            select(DependencyCircuit)
            .where(DependencyCircuit.tenant_id == tenant)
            .order_by(DependencyCircuit.provider, DependencyCircuit.dependency)
        ).all()
        return [
            dict(
                dependency=row.dependency,
                provider=row.provider,
                state=row.state,
                failures=row.failures,
                generation=row.generation,
                retry_at=row.retry_at.isoformat() if row.retry_at else None,
                active_reads=active.get(row.dependency, 0),
                read_limit=row.max_in_flight or service.settings.max_reads_per_dependency,
            )
            for row in rows
        ]


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
                validate_permit(service, permit)
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
