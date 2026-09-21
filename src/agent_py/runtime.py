import asyncio
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from agent_py.adapters.simulation import DisabledLiveExecutor, SimulatedSystem
    from agent_py.config import Settings
    from agent_py.db import Database, Operation, Outbox, Task, emit, tenant_get
    from agent_py.domain import DomainError
    from agent_py.investigation import build_harness
    from agent_py.service import Service


@workflow.defn
class AgentWorkflow:
    @workflow.run
    async def run(self, identity: dict) -> dict:
        for _ in range(1000):
            result = await workflow.execute_activity(
                "agent_tick",
                identity,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1)),
            )
            if result.get("done"):
                return result
            await workflow.sleep(timedelta(seconds=2 if not result.get("wait") else 15))
        workflow.continue_as_new(identity)


class Activities:
    def __init__(self, service):
        self.service, self.harness = service, build_harness(service)

    @activity.defn(name="agent_tick")
    async def tick(self, identity: dict) -> dict:
        import time

        from agent_py.scheduling import acquire, execution_lease, release

        tenant, task_id = identity["tenant"], identity["task_id"]
        token, reason = await asyncio.to_thread(acquire, self.service, tenant, task_id)
        if reason and reason != "CONTROL_ONLY":
            self.service.telemetry.ticks.labels("deferred").inc()
            return {"done": False, "wait": reason}
        context_token = execution_lease.set((tenant, task_id, token) if token else None)
        started = time.monotonic()
        abandoned = False
        try:
            from agent_py.trace_context import enabled, read_links, record_segment

            links = await asyncio.to_thread(
                read_links, self.service, tenant, task_id, identity.get("trace_context")
            )
            with self.service.telemetry.span(
                "worker.tick",
                links=links,
                new_root=enabled(self.service, tenant),
                **{"task.id": task_id, "tenant.id": tenant},
            ) as span:
                await asyncio.to_thread(
                    record_segment, self.service, tenant, task_id, span.get_span_context()
                )
                try:
                    result = await self._tick(identity)
                except BaseException:
                    span.set_attribute("result", "error")
                    raise
                from agent_py.observations import worker_result_attributes

                span.set_attributes(worker_result_attributes(result))
                outcome = (
                    "done"
                    if result.get("done")
                    else "waiting"
                    if result.get("wait")
                    else "progress"
                )
                span.set_attribute("result", outcome)
                self.service.telemetry.ticks.labels(outcome).inc()
                return result
        except asyncio.CancelledError:
            abandoned = True  # to_thread may still be running; retain its slot until lease expiry.
            self.service.telemetry.ticks.labels("abandoned").inc()
            raise
        except Exception:
            self.service.telemetry.ticks.labels("error").inc()
            raise
        finally:
            execution_lease.reset(context_token)
            self.service.telemetry.tick_duration.observe(time.monotonic() - started)
            if token and not abandoned:
                await asyncio.to_thread(release, self.service, tenant, task_id, token)

    async def _tick(self, identity):
        tenant, task_id = identity["tenant"], identity["task_id"]
        try:
            return await asyncio.to_thread(self.harness.tick, tenant, task_id)
        except DomainError as exc:
            with self.service.db.session(tenant) as s:
                t = tenant_get(s, Task, task_id, tenant, True)
                from agent_py.scheduling import check_execution_lease

                try:
                    check_execution_lease(s, t)
                except DomainError:
                    return {"done": t.status == "TERMINATED", "wait": "WORKER_LEASE_LOST"}
                if not t.cancelled and not t.taken_over and t.status != "TERMINATED":
                    if t.status != "WAITING" or t.waiting_reason != exc.code:
                        t.status, t.waiting_reason = "WAITING", exc.code
                        emit(s, t, "task.blocked", {"reason": exc.code})
                done = t.status == "TERMINATED"
            # All in-flight operations remain visible to the independent reconciler.
            return {"done": done, "wait": exc.code, "blocked": exc.code}


def build_service(settings: Settings) -> Service:
    db = Database(settings.database_url)
    if settings.environment == "production":
        db.assert_production_role()
    remote = (
        SimulatedSystem(settings.artifact_root.parent / "authority.db")
        if settings.execution_mode == "simulation"
        else DisabledLiveExecutor()
    )
    return Service(db, settings, remote)


async def dispatch_once(service: Service, client, tenant: str):
    from sqlalchemy import select

    with service.db.session(tenant) as s:
        service.release.check()
        rows = s.scalars(
            select(Outbox)
            .join(Task, (Task.id == Outbox.task_id) & (Task.tenant_id == Outbox.tenant_id))
            .where(
                Outbox.tenant_id == tenant,
                Outbox.delivered.is_(False),
                Task.contract["release_id"].as_string() == service.release.id,
            )
            .order_by(Outbox.created_at, Outbox.id)
            .limit(20)
        ).all()
    for row in rows:
        from agent_py.trace_context import read_links, record_segment

        links = await asyncio.to_thread(read_links, service, tenant, row.task_id)
        with service.telemetry.span(
            "task.dispatch",
            links=links,
            new_root=True,
            **{"tenant.id": tenant, "task.id": row.task_id},
        ) as span:
            carrier = await asyncio.to_thread(
                record_segment, service, tenant, row.task_id, span.get_span_context(), dispatch=True
            )
            identity = {"tenant": tenant, "task_id": row.task_id}
            if carrier is not None:
                identity["trace_context"] = carrier
            try:
                await client.start_workflow(
                    AgentWorkflow.run,
                    identity,
                    id=f"agent:{tenant}:{row.task_id}",
                    task_queue=service.release.task_queue,
                )
            except WorkflowAlreadyStartedError:
                pass
        with service.db.session(tenant) as s:
            tenant_get(s, Outbox, row.id, tenant).delivered = True


def reconcile_once(service: Service, tenant: str):
    from sqlalchemy import select

    with service.db.session(tenant) as s:
        base = select(Operation).where(
            Operation.tenant_id == tenant, Operation.status.in_(["PENDING", "UNKNOWN"])
        )
        cursor = service.reconcile_cursors.get(tenant, "")
        rows = s.scalars(base.where(Operation.id > cursor).order_by(Operation.id).limit(100)).all()
        if not rows and cursor:
            rows = s.scalars(base.order_by(Operation.id).limit(100)).all()
        service.reconcile_cursors[tenant] = rows[-1].id if rows else ""
    for op in rows:
        try:
            service.reconcile(tenant, op.id)
        except Exception as exc:
            # One unavailable provider must not abandon other reconciliation obligations.
            import logging

            logging.getLogger(__name__).error(
                "Reconciliation failed for operation %s: %s", op.id, type(exc).__name__
            )
        finally:
            service.escalate_uncertain(tenant, op.id)


async def serve_worker(settings: Settings):
    from agent_py.metrics_server import start_worker_metrics

    service = build_service(settings)
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    worker = Worker(
        client,
        task_queue=service.release.task_queue,
        workflows=[AgentWorkflow],
        activities=[Activities(service).tick],
        max_concurrent_activities=settings.worker_activity_limit,
    )
    metrics_server = start_worker_metrics(service)
    try:
        await worker.run()
    finally:
        if metrics_server:
            await asyncio.to_thread(metrics_server.shutdown)
            metrics_server.server_close()
        service.telemetry.close()


async def serve_dispatcher(settings: Settings, tenants: list[str]):
    service = build_service(settings)
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    try:
        while True:
            await dispatcher_round(service, client, tenants)
            await asyncio.sleep(2)
    finally:
        service.telemetry.close()


async def dispatcher_round(service, client, tenants):
    import logging

    from agent_py.webhooks import consume

    for tenant in dict.fromkeys(tenants):
        for name, action in (
            ("dispatch", lambda: dispatch_once(service, client, tenant)),
            ("reconcile", lambda: asyncio.to_thread(reconcile_once, service, tenant)),
            ("inbox", lambda: asyncio.to_thread(consume, service, tenant)),
        ):
            try:
                await action()
            except Exception as exc:
                # Do not log provider response bodies or credentials from exception messages.
                logging.getLogger(__name__).error(
                    "Tenant %s stage %s failed: %s", tenant, name, type(exc).__name__
                )
