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
        tenant, task_id = identity["tenant"], identity["task_id"]
        try:
            return await asyncio.to_thread(self.harness.tick, tenant, task_id)
        except DomainError as exc:
            with self.service.db.session(tenant) as s:
                t = tenant_get(s, Task, task_id, tenant, True)
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
        rows = s.scalars(
            select(Outbox).where(Outbox.tenant_id == tenant, Outbox.delivered.is_(False)).limit(100)
        ).all()
    for row in rows:
        try:
            await client.start_workflow(
                AgentWorkflow.run,
                {"tenant": tenant, "task_id": row.task_id},
                id=f"agent:{tenant}:{row.task_id}",
                task_queue=service.settings.task_queue,
            )
        except WorkflowAlreadyStartedError:
            pass
        with service.db.session(tenant) as s:
            tenant_get(s, Outbox, row.id, tenant).delivered = True


def reconcile_once(service: Service, tenant: str):
    from sqlalchemy import select

    with service.db.session(tenant) as s:
        rows = s.scalars(
            select(Operation)
            .where(Operation.tenant_id == tenant, Operation.status.in_(["PENDING", "UNKNOWN"]))
            .limit(100)
        ).all()
    for op in rows:
        try:
            service.reconcile(tenant, op.id)
        except Exception:
            # One unavailable provider must not abandon other reconciliation obligations.
            import logging

            logging.getLogger(__name__).exception("Reconciliation failed for operation %s", op.id)
        finally:
            service.escalate_uncertain(tenant, op.id)


async def serve_worker(settings: Settings):
    service = build_service(settings)
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[AgentWorkflow],
        activities=[Activities(service).tick],
        max_concurrent_activities=8,
    )
    await worker.run()


async def serve_dispatcher(settings: Settings, tenants: list[str]):
    service = build_service(settings)
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    while True:
        for tenant in tenants:
            await dispatch_once(service, client, tenant)
            await asyncio.to_thread(reconcile_once, service, tenant)
            from agent_py.webhooks import consume

            await asyncio.to_thread(consume, service, tenant)
        await asyncio.sleep(2)
