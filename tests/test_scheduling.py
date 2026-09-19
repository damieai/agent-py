import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import func, select

from agent_py.db import Task, WorkLease, now
from agent_py.domain import DomainError, TaskContract
from agent_py.runtime import Activities
from agent_py.scheduling import acquire, execution_lease, release


def create(env, key, tenant="t1"):
    service, p, _ = env
    return service.create_task(
        p.model_copy(update={"tenant_id": tenant}),
        TaskContract(kind="repair", project="demo", goal="Exercise worker admission"),
        key,
    )


def test_capacity_fifo_and_tenant_independence(env):
    service = env[0]
    service.settings.max_active_per_tenant = 1
    first, waiting, newcomer = [create(env, str(i)) for i in range(3)]
    token, _ = acquire(service, "t1", first.id)
    assert token
    assert acquire(service, "t1", first.id)[1] == "DUPLICATE_TICK"
    assert acquire(service, "t1", waiting.id)[1] == "TENANT_CAPACITY"
    other = create(env, "other", "t2")
    assert acquire(service, "t2", other.id)[0]
    release(service, "t1", first.id, token)
    assert acquire(service, "t1", newcomer.id)[1] == "TENANT_QUEUE"
    assert acquire(service, "t1", waiting.id)[0]


def test_cross_worker_race_respects_one_slot(env):
    service = env[0]
    service.settings.max_active_per_tenant = 1
    tasks = [create(env, str(i)) for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda t: acquire(service, "t1", t.id), tasks))
    assert sum(bool(token) for token, _ in results) == 1
    with service.db.session("t1") as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkLease)
                .where(WorkLease.owner_token.is_not(None))
            )
            == 1
        )


def test_stale_worker_cannot_dispatch_or_release_new_owner(env, task):
    service = env[0]
    old, _ = acquire(service, "t1", task.id)
    with service.db.session("t1") as s:
        s.scalar(select(WorkLease)).expires_at = now() - timedelta(seconds=1)
    new, _ = acquire(service, "t1", task.id)
    assert new and new != old
    release(service, "t1", task.id, old)
    context = execution_lease.set(("t1", task.id, old))
    try:
        with pytest.raises(DomainError, match="superseded"):
            service.reserve("t1", task.id, "stale", 100)
    finally:
        execution_lease.reset(context)
    context = execution_lease.set(("t1", task.id, new))
    try:
        assert service.reserve("t1", task.id, "valid", 100)
    finally:
        execution_lease.reset(context)


def test_dead_waiter_expires_and_cancelled_waiter_does_not_block(env):
    service, p, _ = env
    service.settings.max_active_per_tenant = 1
    one, two, three = [create(env, str(i)) for i in range(3)]
    token, _ = acquire(service, "t1", one.id)
    acquire(service, "t1", two.id)
    service.stop(p, two.id)
    release(service, "t1", one.id, token)
    assert acquire(service, "t1", three.id)[0]
    with service.db.session("t1") as s:
        for lease in s.scalars(select(WorkLease)):
            lease.expires_at = now() - timedelta(seconds=1)
    assert acquire(service, "t1", one.id)[0]


def test_queue_capacity_is_atomic_and_idempotent(env):
    service = env[0]
    service.settings.max_queue_per_tenant = 3

    def submit(i):
        try:
            return create(env, str(i)).id
        except DomainError as exc:
            assert exc.code == "QUEUE_FULL"
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert sum(r is not None for r in results) == 3
    with service.db.session("t1") as s:
        saved = s.scalar(select(Task))
    assert create(env, saved.request_key).id == saved.id


def test_waiting_tick_releases_capacity(env, task):
    service = env[0]
    service.settings.execution_mode = "live"
    activities = Activities(service)
    result = asyncio.run(activities.tick({"tenant": "t1", "task_id": task.id}))
    assert result["wait"] == "MODEL_API_DISABLED"
    with service.db.session("t1") as s:
        assert s.scalar(select(func.count()).select_from(WorkLease)) == 0


def test_activity_cancellation_retains_slot_until_expiry(env, task):
    service = env[0]
    started, finish = Event(), Event()
    activities = Activities(service)

    def slow(*args):
        started.set()
        finish.wait(5)
        return {"done": False}

    activities.harness.tick = slow

    async def scenario():
        execution = asyncio.create_task(activities.tick({"tenant": "t1", "task_id": task.id}))
        assert await asyncio.to_thread(started.wait, 5)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert acquire(service, "t1", task.id)[1] == "DUPLICATE_TICK"
        finish.set()

    asyncio.run(scenario())


def test_shared_policy_does_not_change_with_worker_local_config(env):
    from agent_py.operations import summary

    service = env[0]
    service.settings.max_active_per_tenant = 1
    first, other = create(env, "first"), create(env, "other")
    assert acquire(service, "t1", first.id)[0]
    service.settings.max_active_per_tenant = 8
    assert acquire(service, "t1", other.id)[1] == "TENANT_CAPACITY"
    assert summary(service, "t1")["admission"]["limit"] == 1


def test_duplicate_submission_survives_full_queue_race(env):
    service = env[0]
    service.settings.max_queue_per_tenant = 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        identifiers = list(pool.map(lambda _: create(env, "same-key").id, range(4)))
    assert len(set(identifiers)) == 1


def test_late_worker_error_does_not_overwrite_replacement_state(env, task):
    service = env[0]
    activities = Activities(service)

    def superseded(*args):
        with service.db.session("t1") as s:
            s.scalar(select(WorkLease)).expires_at = now() - timedelta(seconds=1)
        assert acquire(service, "t1", task.id)[0]
        with service.db.session("t1") as s:
            current = s.get(Task, task.id)
            current.status, current.waiting_reason = "RUNNING", None
        raise DomainError("OLD_FAILURE", "Late worker failure")

    activities.harness.tick = superseded
    result = asyncio.run(activities.tick({"tenant": "t1", "task_id": task.id}))
    assert result["wait"] == "WORKER_LEASE_LOST"
    with service.db.session("t1") as s:
        assert s.get(Task, task.id).status == "RUNNING"
        assert s.scalar(select(WorkLease)).owner_token is not None
