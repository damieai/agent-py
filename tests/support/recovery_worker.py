"""Isolated fault-injection process. Never registered by the production worker."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from agent_py.adapters.simulation import SimulatedSystem
from agent_py.config import Settings
from agent_py.db import Database
from agent_py.harness import SimulationHarness
from agent_py.runtime import Activities, AgentWorkflow
from agent_py.service import Service


def main():
    if os.environ.get("AGENT_RECOVERY_FIXTURE") != "1":
        raise SystemExit("Explicit recovery fixture opt-in required")
    config = json.loads(Path(sys.argv[1]).read_text())
    root = Path(config["root"])
    for name in list(os.environ):
        if name.startswith("AGENT_"):
            del os.environ[name]
    settings = Settings(
        environment="test",
        execution_mode="simulation",
        database_url=f"sqlite:///{root / 'app.db'}",
        artifact_root=root / "artifacts",
        auth_secret="recovery-fixture-only-" * 3,
        _env_file=None,
    )

    def crash(phase):
        with (root / "crash.json").open("w") as stream:
            json.dump({"phase": phase, "pid": os.getpid()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.kill(os.getpid(), signal.SIGKILL)
        raise AssertionError("SIGKILL returned")

    class CrashAuthority(SimulatedSystem):
        def execute(self, *args, **kwargs):
            if config.get("phase") == "before_remote_commit":
                crash("before_remote_commit")
            result = super().execute(*args, **kwargs)
            if config.get("phase") == "after_remote_commit":
                crash("after_remote_commit")
            return result

    service = Service(
        Database(settings.database_url), settings, CrashAuthority(root / "authority.db")
    )

    class CrashHarness(SimulationHarness):
        def tick(self, *args):
            result = super().tick(*args)
            if config.get("phase") == "after_local_commit" and result.get("operation_id"):
                crash("after_local_commit")
            return result

    activities = Activities(service)
    activities.harness = CrashHarness(service)

    async def run():
        if config["mode"] == "tick":
            await activities.tick({"tenant": "recovery", "task_id": config["task_id"]})
            raise AssertionError("Fault boundary was not reached")
        if config["mode"] != "worker":
            raise ValueError("Unknown fixture mode")
        client = await Client.connect(config["address"])
        async with Worker(
            client,
            task_queue=config["queue"],
            workflows=[AgentWorkflow],
            activities=[activities.tick],
        ):
            print("RECOVERY_WORKER_READY", flush=True)
            await asyncio.Event().wait()

    try:
        asyncio.run(run())
    finally:
        service.db.engine.dispose()
        service.telemetry.close()


if __name__ == "__main__":
    main()
