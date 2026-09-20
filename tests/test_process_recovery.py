"""Hard process death against durable local ledgers and a native Temporal server."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from agent_py.adapters.simulation import SimulatedSystem
from agent_py.config import Settings
from agent_py.db import Approval, Database, Grant, Operation, Task, WorkLease
from agent_py.domain import Principal, TaskContract
from agent_py.runtime import Activities, AgentWorkflow, reconcile_once
from agent_py.service import Service

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AGENT_TEST_RECOVERY") != "1", reason="Native process recovery opt-in"
    ),
    pytest.mark.skipif(sys.platform != "linux", reason="SIGKILL drill requires Linux"),
]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    # Keep only test controls, even when someone invokes pytest without the gate wrapper.
    for name in list(os.environ):
        if name.startswith("AGENT_") and name not in {
            "AGENT_TEST_RECOVERY",
            "AGENT_RECOVERY_REPORT_DIR",
        }:
            monkeypatch.delenv(name)


def setup(root):
    settings = Settings(
        environment="test",
        execution_mode="simulation",
        database_url=f"sqlite:///{root / 'app.db'}",
        artifact_root=root / "artifacts",
        auth_secret="recovery-fixture-only-" * 3,
        _env_file=None,
    )
    db = Database(settings.database_url)
    db.create_schema()
    service = Service(db, settings, SimulatedSystem(root / "authority.db"))
    owner = Principal(
        tenant_id="recovery",
        subject="owner",
        roles=["developer", "operator"],
        projects=["demo"],
        environments=["lab"],
    )
    reviewer = owner.model_copy(update={"subject": "reviewer", "roles": ["approver", "operator"]})
    with db.session("recovery") as s:
        for subject in ("owner", "reviewer"):
            s.add(Grant(tenant_id="recovery", subject=subject, project="demo", environment="lab"))
    task = service.create_task(
        owner,
        TaskContract(
            kind="repair",
            goal="Recover a hard-killed worker",
            project="demo",
        ),
        "recovery-task",
    )
    return service, owner, reviewer, task


def launch(root, name, **config):
    path = root / f"{name}.json"
    path.write_text(json.dumps({"root": str(root), **config}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
    env["AGENT_RECOVERY_FIXTURE"] = "1"
    log = root / f"{name}.log"
    with log.open("wb") as stream:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "tests/support/recovery_worker.py"), str(path)],
            env=env,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    return process, log


def cleanup(process):
    if process is not None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def evidence(name, values):
    directory = os.getenv("AGENT_RECOVERY_REPORT_DIR")
    if directory:
        Path(directory, name + ".json").write_text(json.dumps(values, indent=2, sort_keys=True))


@pytest.mark.parametrize(
    "phase", ["before_remote_commit", "after_remote_commit", "after_local_commit"]
)
def test_hard_kill_action_boundary(tmp_path, phase):
    service, owner, _, task = setup(tmp_path)
    process = None
    started = time.monotonic()
    try:
        process, log = launch(tmp_path, "crashing", mode="tick", phase=phase, task_id=task.id)
        assert process.wait(timeout=30) == -signal.SIGKILL, log.read_text()
        assert json.loads((tmp_path / "crash.json").read_text())["phase"] == phase
        # Drop the parent's pool too: no in-memory state is allowed to recover the task.
        service.db.engine.dispose()
        service = Service(
            Database(service.settings.database_url),
            service.settings,
            SimulatedSystem(tmp_path / "authority.db"),
        )
        with service.db.session("recovery") as s:
            op = s.scalar(select(Operation).where(Operation.task_id == task.id))
            assert op is not None and op.attempts == 1
            before = op.status
            assert before == ("SUCCEEDED" if phase == "after_local_commit" else "PENDING")
            lease = s.scalar(select(WorkLease).where(WorkLease.task_id == task.id))
            assert lease.owner_token is not None  # SIGKILL did not execute finally/release.
        expected_effects = 0 if phase == "before_remote_commit" else 1
        assert (
            service.remote.snapshot("recovery", task.contract["resource"])["effect_count"]
            == expected_effects
        )
        # A replacement cannot bypass the old lease. Never edit deadlines/leases in the drill.
        deferred = asyncio.run(Activities(service).tick({"tenant": "recovery", "task_id": task.id}))
        assert deferred["wait"] == "DUPLICATE_TICK"
        for _ in range(3):
            reconcile_once(service, "recovery")
        with service.db.session("recovery") as s:
            recovered = s.get(Operation, op.id)
            assert recovered.status == ("UNKNOWN" if expected_effects == 0 else "SUCCEEDED")
            assert recovered.attempts == 1
        # Cancellation still drains known results; absent receipts must remain unresolved.
        service.stop(owner, task.id)
        result = asyncio.run(Activities(service).tick({"tenant": "recovery", "task_id": task.id}))
        assert result.get("result") == ("CANCELLED" if expected_effects else None)
        if not expected_effects:
            assert result["wait"] == "RECONCILIATION"
            assert service.get_task(owner, task.id).result is None
        assert (
            service.remote.snapshot("recovery", task.contract["resource"])["effect_count"]
            == expected_effects
        )
        evidence(
            phase,
            {
                "scenario": phase,
                "passed": True,
                "worker_exit": process.returncode,
                "before": before,
                "after": recovered.status,
                "attempts": recovered.attempts,
                "remote_effect_count": expected_effects,
                "replacement_wait": deferred["wait"],
                "cancel_result": result,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "worker_lease_seconds": service.settings.worker_lease_seconds,
                "scope": "real_SIGKILL_SQLite_and_simulated_authority_not_Temporal_activity_retry",
            },
        )
    finally:
        cleanup(process)
        service.db.engine.dispose()
        service.telemetry.close()


async def wait_until(predicate, timeout, process=None, log=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise AssertionError(f"Worker exited {process.returncode}: {log.read_text()}")
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.2)
    raise AssertionError("Recovery drill exceeded bounded wait")


def test_temporal_worker_process_restart_and_history_replay(tmp_path):
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer

    service, owner, reviewer, task = setup(tmp_path)
    first = second = None
    started = time.monotonic()

    async def scenario():
        nonlocal first, second
        async with await WorkflowEnvironment.start_local() as runtime:
            queue = "recovery-" + task.id
            config = {
                "mode": "worker",
                "address": runtime.client.service_client.config.target_host,
                "queue": queue,
            }
            first, log = launch(tmp_path, "first-worker", **config)
            await wait_until(lambda: "RECOVERY_WORKER_READY" in log.read_text(), 20, first, log)
            handle = await runtime.client.start_workflow(
                AgentWorkflow.run,
                {"tenant": "recovery", "task_id": task.id},
                id="process-recovery-" + task.id,
                task_queue=queue,
            )

            def pending_approval():
                with service.db.session("recovery") as s:
                    # No active lease: the Activity has durably reached approval waiting.
                    if s.scalar(select(WorkLease.id).where(WorkLease.task_id == task.id)):
                        return None
                    return s.scalar(select(Approval).where(Approval.status == "PENDING"))

            approval = await wait_until(pending_approval, 30, first, log)
            # Wait for the server to record the durable wait timer, not just the SQL row.
            from temporalio.api.enums.v1 import EventType

            deadline = time.monotonic() + 10
            while True:
                history = await handle.fetch_history()
                if history.events[-1].event_type == EventType.EVENT_TYPE_TIMER_STARTED:
                    break
                assert time.monotonic() < deadline, "No durable wait timer"
                await asyncio.sleep(0.2)
            first.kill()
            assert await asyncio.to_thread(first.wait, 10) == -signal.SIGKILL
            with service.db.session("recovery") as s:
                before_ids = list(
                    s.scalars(select(Operation.id).where(Operation.task_id == task.id))
                )
            service.decide(reviewer, approval.id, "approve", approval.payload_digest)
            restart = time.monotonic()
            second, second_log = launch(tmp_path, "replacement-worker", **config)
            await wait_until(
                lambda: "RECOVERY_WORKER_READY" in second_log.read_text(), 20, second, second_log
            )
            deadline = time.monotonic() + 65
            while time.monotonic() < deadline:
                assert second.poll() is None, second_log.read_text()
                with service.db.session("recovery") as s:
                    approvals = list(
                        s.scalars(select(Approval).where(Approval.status == "PENDING"))
                    )
                    done = s.get(Task, task.id).status == "TERMINATED"
                for pending in approvals:
                    service.decide(reviewer, pending.id, "approve", pending.payload_digest)
                if done:
                    break
                await asyncio.sleep(0.2)
            result = await asyncio.wait_for(handle.result(), 10)
            assert result["result"] == "SUCCESS"
            history = await handle.fetch_history()
            replay = await Replayer(workflows=[AgentWorkflow]).replay_workflow(history)
            assert replay.replay_failure is None
            if directory := os.getenv("AGENT_RECOVERY_REPORT_DIR"):
                Path(directory, "temporal-history.json").write_text(history.to_json())
            with service.db.session("recovery") as s:
                ops = list(s.scalars(select(Operation).where(Operation.task_id == task.id)))
                assert len(ops) == 4 and set(before_ids) <= {op.id for op in ops}
                assert all(op.attempts == 1 and op.status == "SUCCEEDED" for op in ops)
            assert (
                service.remote.snapshot("recovery", task.contract["resource"])["effect_count"] == 4
            )
            evidence(
                "temporal_restart",
                {
                    "scenario": "temporal_restart",
                    "passed": True,
                    "first_exit": first.returncode,
                    "first_pid": first.pid,
                    "replacement_pid": second.pid,
                    "workflow_id": handle.id,
                    "result": result["result"],
                    "operation_count": len(ops),
                    "attempts": [op.attempts for op in ops],
                    "remote_effect_count": 4,
                    "history_events": len(history.events),
                    "history_replay": "passed",
                    "restart_to_completion_seconds": round(time.monotonic() - restart, 3),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "scope": "native_Temporal_restart_at_durable_wait_same_code_not_old_version_upgrade",
                },
            )

    try:
        asyncio.run(scenario())
    finally:
        cleanup(first)
        cleanup(second)
        service.db.engine.dispose()
        service.telemetry.close()
