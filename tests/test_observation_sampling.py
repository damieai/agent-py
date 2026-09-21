"""Task-coherent sampling must not alter execution or persistence semantics."""

import asyncio
import json
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

pytest.importorskip("langfuse")
from test_langfuse import decode, settings
from test_trace_context import configure

from agent_py.config import Settings
from agent_py.db import TaskTrace
from agent_py.domain import TaskContract
from agent_py.langfuse_export import NAMES, STAGES
from agent_py.observation_policy import selected
from agent_py.runtime import dispatch_once


@pytest.mark.parametrize("rate", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_sampling_rate_rejected(rate):
    with pytest.raises(ValidationError):
        Settings(langfuse_sample_rate=rate, _env_file=None)


def test_selection_stable_across_fresh_processes_and_monotonic():
    config = settings(langfuse_sample_rate=0.25)
    identifiers = [f"task-{i}" for i in range(100)]
    expected = [selected(config, "t1", task) for task in identifiers]
    config.langfuse_sample_rate = 0.75
    larger = [selected(config, "t1", task) for task in identifiers]
    assert any(expected) and not all(larger)
    assert all(not a or b for a, b in zip(expected, larger))
    program = """import json
from agent_py.config import Settings
from agent_py.observation_policy import selected
s = Settings.model_validate(json.loads(input()))
print(json.dumps([selected(s, "t1", f"task-{i}") for i in range(100)]))
"""
    # Serialize only the explicitly created fixture config, including its dummy SecretStrs.
    data = config.model_dump(mode="json")
    for field in ("langfuse_public_key", "langfuse_secret_key", "langfuse_pseudonym_key"):
        data[field] = getattr(config, field).get_secret_value()
    result = subprocess.run(
        [sys.executable, "-c", program],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == larger
    assert not selected(config, "t2", "task-1")


def test_entire_task_sampled_together_before_serialization(env, monkeypatch):
    requests, processors = configure(env, monkeypatch)
    service = env[0]
    service.settings.langfuse_sample_rate = 0.5
    yes = next(f"task-{i}" for i in range(100) if selected(service.settings, "t1", f"task-{i}"))
    no = next(f"task-{i}" for i in range(100) if not selected(service.settings, "t1", f"task-{i}"))
    original = processors[0].sanitize
    sanitized = []

    def capture(span):
        sanitized.append(span.attributes["task.id"])
        return original(span)

    monkeypatch.setattr(processors[0], "sanitize", capture)
    try:
        names = NAMES | STAGES.keys()
        for task in (yes, no):
            for name in names:
                with service.telemetry.span(name, **{"tenant.id": "t1", "task.id": task}):
                    pass
        service.telemetry.close()
        assert sanitized == [yes] * len(names)
        assert {s.name for s, _, _ in decode(requests)} == names
        assert len(decode(requests)) == len(names)
        assert service.telemetry.langfuse_events.labels("sampled_out")._value.get() == len(names)
    finally:
        service.telemetry.close()


@pytest.mark.parametrize("rate", [0, 1])
def test_sampling_controls_trace_rows_and_carrier_not_task_dispatch(env, monkeypatch, rate):
    requests, _ = configure(env, monkeypatch)
    service, principal, _ = env
    service.settings.langfuse_sample_rate = rate
    try:
        task = service.create_task(
            principal, TaskContract(kind="repair", project="demo", goal="fixture"), "sampled"
        )
        client = AsyncMock()
        asyncio.run(dispatch_once(service, client, "t1"))
        identity = client.start_workflow.call_args.args[1]
        assert identity["task_id"] == task.id
        assert ("trace_context" in identity) == bool(rate)
        with service.db.session("t1") as session:
            assert session.scalar(select(func.count()).select_from(TaskTrace)) == rate
        service.telemetry.close()
        assert len(decode(requests)) == rate * 2
    finally:
        service.telemetry.close()
