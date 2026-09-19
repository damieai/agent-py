import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from agent_py.db import Outbox
from agent_py.runtime import Activities, AgentWorkflow, dispatch_once


def test_outbox_marks_only_after_start_ack(env, task):
    service = env[0]
    client = AsyncMock()
    client.start_workflow.side_effect = ConnectionError("start result lost")
    with pytest.raises(ConnectionError):
        asyncio.run(dispatch_once(service, client, "t1"))
    with service.db.session("t1") as s:
        assert not s.scalar(select(Outbox).where(Outbox.task_id == task.id)).delivered
    client.start_workflow.side_effect = None
    asyncio.run(dispatch_once(service, client, "t1"))
    with service.db.session("t1") as s:
        assert s.scalar(select(Outbox).where(Outbox.task_id == task.id)).delivered


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("AGENT_TEST_TEMPORAL") != "1", reason="Temporal test server opt-in")
def test_temporal_runs_until_approval(env, task):
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from agent_py.db import Approval, Task

    async def scenario():
        service, _, reviewer = env
        async with await WorkflowEnvironment.start_local() as runtime:
            async with Worker(
                runtime.client,
                task_queue="test",
                workflows=[AgentWorkflow],
                activities=[Activities(service).tick],
            ):
                handle = await runtime.client.start_workflow(
                    AgentWorkflow.run,
                    {"tenant": "t1", "task_id": task.id},
                    id=task.id,
                    task_queue="test",
                )
                for _ in range(90):
                    with service.db.session("t1") as s:
                        pending = list(
                            s.scalars(select(Approval).where(Approval.status == "PENDING"))
                        )
                        done = s.get(Task, task.id).status == "TERMINATED"
                    for a in pending:
                        service.decide(reviewer, a.id, "approve", a.payload_digest)
                    if done:
                        break
                    await asyncio.sleep(1)
                result = await asyncio.wait_for(handle.result(), 15)
                assert result["result"] == "SUCCESS"

    asyncio.run(scenario())
