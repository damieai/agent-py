import asyncio
import copy
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from agent_py.api import create_app
from agent_py.audit import check_recording, export_audit
from agent_py.cli import app
from agent_py.config import Settings
from agent_py.db import Database, Grant, Outbox, Policy, Task
from agent_py.domain import ActionProposal, DomainError, TaskContract, digest
from agent_py.evaluation import dataset
from agent_py.releases import (
    AgentRelease,
    GateEvidence,
    ReleaseGuard,
    create_release,
    read_regular,
    release_id,
    write_release,
)
from agent_py.retrieval_evaluation import evaluate_retrieval
from agent_py.runtime import dispatch_once
from agent_py.service import Service

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def release_evidence(tmp_path_factory):
    root = tmp_path_factory.mktemp("release-evidence")
    retrieval = evaluate_retrieval(ROOT / "examples/retrieval-development.json", root / "r.json")
    cases = [c for c in dataset() if c["split"] == "development"]
    # Gate contract fixture, explicitly not an actual execution/provenance attestation.
    simulation = dict(
        suite="simulation-conformance-v2",
        dataset_digest=digest(cases),
        not_a_model_benchmark=True,
        dataset_limitation="Synthetic release test evidence",
        split="development",
        passed=len(cases),
        total=len(cases),
        results=[
            dict(
                id=c["id"],
                family=c["family"],
                task_id=c["id"],
                expected=c["expected"],
                actual=c["expected"],
                passed=True,
                confirmed_operations=1,
                remote_effects=1,
                maximum_attempts=1,
                simulation=True,
            )
            for c in cases
        ],
    )
    return GateEvidence(
        policy=json.loads((ROOT / "examples/evaluation-gate-policy.json").read_text()),
        simulation=simulation,
        baseline=retrieval,
        candidate=retrieval,
        fixture=json.loads((ROOT / "examples/retrieval-development.json").read_text()),
    )


@pytest.fixture
def pinned(env, tmp_path, release_evidence):
    original, owner, reviewer = env
    settings = original.settings.model_copy(update={"context_strategy": "bm25_rrf"})
    manifest = create_release(settings, ROOT, release_evidence)
    path = tmp_path / "release.json"
    write_release(manifest, path)
    settings = settings.model_copy(
        update={
            "release_manifest": path,
            "release_expected_id": release_id(manifest),
            "release_root": ROOT,
        }
    )
    service = Service(original.db, settings, original.remote)
    yield service, owner, reviewer, manifest
    service.telemetry.close()


def contract(**kwargs):
    return TaskContract(kind="repair", goal="Check release isolation", project="demo", **kwargs)


def test_creation_pins_release_and_audit_binds_all_copies(pinned):
    service, owner, _, manifest = pinned
    task = service.create_task(owner, contract(), "release")
    assert task.contract["release_id"] == release_id(manifest)
    assert service.create_task(owner, contract(), "release").id == task.id
    recording = export_audit(service, owner, task.id)
    check_recording(recording)
    assert recording["body"]["release"] == task.contract["release_id"]
    recording["body"]["release"] = "sha256:" + "f" * 64
    recording["digest"] = digest(recording["body"])
    with pytest.raises(DomainError, match="release"):
        check_recording(recording)


def test_legacy_request_retry_preserves_original_release_after_upgrade(env, task, pinned):
    original, owner, _ = env
    upgraded = pinned[0]
    old_contract = TaskContract.model_validate(task.contract)
    assert upgraded.create_task(owner, old_contract, task.request_key).id == task.id
    with pytest.raises(DomainError, match="another release"):
        upgraded.reserve("t1", task.id, "upgrade", 1)
    assert original.reserve("t1", task.id, "legacy", 1)


def test_unknown_explicit_release_rejected_before_creation(pinned):
    service, owner, _, _ = pinned
    with pytest.raises(DomainError, match="Requested release"):
        service.create_task(owner, contract(release_id="sha256:" + "f" * 64), "wrong")
    with service.db.session("t1") as s:
        assert s.scalar(select(Task)) is None


def test_unpinned_worker_cannot_execute_pinned_task(env, pinned):
    service, owner, _, _ = pinned
    task = service.create_task(owner, contract(), "pin")
    with pytest.raises(DomainError, match="another release"):
        env[0].reserve("t1", task.id, "wrong-worker", 1)
    assert service.reserve("t1", task.id, "matching-worker", 1)


def test_dispatcher_filters_before_limit_and_uses_release_queue(env, pinned):
    old, owner, _ = env
    for i in range(23):
        old.create_task(owner, contract(), f"legacy-{i}")
    service = pinned[0]
    task = service.create_task(owner, contract(), "pinned")
    client = AsyncMock()
    asyncio.run(dispatch_once(service, client, "t1"))
    client.start_workflow.assert_awaited_once()
    call = client.start_workflow.call_args
    assert call.kwargs["id"] == f"agent:t1:{task.id}"
    assert call.kwargs["task_queue"] == service.release.task_queue
    assert service.release.id[7:] in service.release.task_queue
    with service.db.session("t1") as s:
        assert len(list(s.scalars(select(Outbox).where(Outbox.delivered.is_(False))))) == 23


@pytest.mark.parametrize(
    "field,value",
    [
        ("context_strategy", "lexical"),
        ("model_id", "changed"),
        ("model_input_micro_per_token", 77),
        ("execution_mode", "live"),
        ("task_queue", "new-queue"),
        ("sandbox_timeout_seconds", 30),
    ],
)
def test_runtime_drift_refuses_new_work(pinned, field, value):
    service, owner, _, _ = pinned
    task = service.create_task(owner, contract(), "drift")
    setattr(service.settings, field, value)
    with pytest.raises(DomainError) as error:
        service.reserve("t1", task.id, "drift", 1)
    assert error.value.code == "RELEASE_MISMATCH"
    with pytest.raises(DomainError):
        service.create_task(owner, contract(), "new")


@pytest.mark.parametrize("control", ["grant", "stop", "cancel", "takeover"])
def test_pinning_does_not_freeze_live_authorization(pinned, control):
    service, owner, _, _ = pinned
    task = service.create_task(owner, contract(), "controls")
    if control == "grant":
        with service.db.session("t1") as s:
            s.scalar(select(Grant).where(Grant.subject == owner.subject)).revoked = True
    elif control == "stop":
        with service.db.session("t1") as s:
            s.add(Policy(tenant_id="t1", stopped=True))
    else:
        service.stop(owner, task.id, takeover=control == "takeover")
    with pytest.raises(DomainError) as error:
        service.reserve("t1", task.id, "blocked", 1)
    assert error.value.code != "RELEASE_MISMATCH"


def test_release_mismatch_does_not_block_late_remote_reconciliation(env, pinned):
    service, owner, _, _ = pinned
    task = service.create_task(owner, contract(), "unknown")
    op = service.propose(
        owner,
        task.id,
        "pr",
        ActionProposal(
            tool="create_pr",
            resource=task.contract["resource"],
            parameters={"candidate_sha": "a" * 40, "base_sha": "b" * 40},
        ),
    )
    service.remote.inject("t1", op.id, "response_lost")
    assert service.execute("t1", op.id).status == "UNKNOWN"
    # A recovery-only process may query old evidence even without that execution release.
    assert env[0].reconcile("t1", op.id).status == "SUCCEEDED"
    assert service.remote.snapshot("t1", task.contract["resource"])["effect_count"] == 1


def test_readiness_reports_configuration_drift(pinned):
    service = pinned[0]
    app = create_app(service.settings, service.db, service.remote)
    with TestClient(app) as client:
        assert client.get("/health/live").json()["release_id"] == service.release.id
        assert client.get("/health/ready").status_code == 200
        service.settings.context_strategy = "lexical"
        assert client.get("/health/ready").status_code == 503


@pytest.mark.parametrize("change", ["pin", "source", "gate", "strategy", "extra", "bindings"])
def test_invalid_release_fails_startup_before_service(pinned, change):
    service, _, _, manifest = pinned
    value = manifest.model_dump(by_alias=True)
    settings = service.settings.model_copy()
    if change == "pin":
        settings.release_expected_id = "sha256:" + "f" * 64
    elif change == "source":
        value["files"]["src/agent_py/model.py"] = "0" * 64
    elif change == "gate":
        value["evidence"]["simulation"]["results"][0]["maximum_attempts"] = 2
    elif change == "strategy":
        value["runtime"]["context_strategy"] = "lexical"
    elif change == "bindings":
        value["bindings"]["collection_manifest"] = "0" * 64
    else:
        value["unexpected"] = True
    settings.release_manifest.write_text(json.dumps(value))
    if change != "pin":
        settings.release_expected_id = "sha256:" + digest(value)
    with pytest.raises(DomainError):
        ReleaseGuard(settings)


def test_configured_file_change_is_rechecked(env, tmp_path, release_evidence):
    original = env[0]
    binding = tmp_path / "collection.json"
    binding.write_text('{"bindings": []}')
    settings = original.settings.model_copy(
        update={
            "context_strategy": "bm25_rrf",
            "collection_manifest": binding,
        }
    )
    release = create_release(settings, ROOT, release_evidence)
    path = tmp_path / "manifest.json"
    write_release(release, path)
    settings = settings.model_copy(
        update={
            "release_manifest": path,
            "release_expected_id": release_id(release),
            "release_root": ROOT,
        }
    )
    guard = ReleaseGuard(settings)
    binding.write_text('{"bindings": ["changed"]}')
    with pytest.raises(DomainError):
        guard.check()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "large", "missing"])
def test_release_input_rejects_unsafe_files(tmp_path, kind):
    path = tmp_path / "input"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("{}")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "large":
        path.write_bytes(b"123456")
    with pytest.raises((OSError, ValueError)):
        read_regular(path, limit=5)


def test_build_requires_passing_matching_candidate(env, release_evidence):
    with pytest.raises(DomainError, match="strategy"):
        create_release(env[0].settings, ROOT, release_evidence)
    altered = copy.deepcopy(release_evidence)
    altered.policy["minimum_recall"] = 1.1
    with pytest.raises(DomainError, match="evidence"):
        create_release(env[0].settings, ROOT, altered)


def test_release_output_is_exclusive_and_private(pinned, tmp_path):
    release = pinned[3]
    path = tmp_path / "copy.json"
    write_release(release, path)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_release(release, path)
    assert release_id(AgentRelease.model_validate(json.loads(path.read_text()))) == release_id(
        release
    )


def test_cli_checks_without_database_or_network(pinned, monkeypatch):
    service = pinned[0]
    monkeypatch.setattr("agent_py.cli.get_settings", lambda: service.settings)
    monkeypatch.setattr(Database, "__init__", lambda *a, **k: pytest.fail("Database accessed"))
    result = CliRunner().invoke(
        app,
        [
            "release-check",
            str(service.settings.release_manifest),
            service.release.id,
            "--root",
            str(ROOT),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["production_ready"] is False
    bad = CliRunner().invoke(
        app,
        [
            "release-check",
            str(service.settings.release_manifest),
            "sha256:" + "f" * 64,
            "--root",
            str(ROOT),
        ],
    )
    assert bad.exit_code == 1


def test_settings_require_manifest_and_external_pin_together(tmp_path):
    with pytest.raises(ValueError, match="together"):
        Settings(release_manifest=tmp_path / "release.json", _env_file=None)
    with pytest.raises(ValueError, match="together"):
        Settings(release_expected_id="sha256:" + "a" * 64, _env_file=None)


def test_service_restart_preserves_release_and_control_only_recovery(pinned):
    service, owner, _, _ = pinned
    task = service.create_task(owner, contract(), "restart")
    restored = Service(service.db, service.settings.model_copy(), service.remote)
    try:
        assert restored.release.id == service.release.id
        assert restored.release.task_queue == service.release.task_queue
        assert restored.reserve("t1", task.id, "after-restart", 1)
        service.settings.model_id = "drift"
        service.stop(owner, task.id)
        assert service.finish("t1", task.id).result == "CANCELLED"
    finally:
        restored.telemetry.close()


def test_release_cli_build_recomputes_evidence_and_refuses_overwrite(
    env, release_evidence, tmp_path, monkeypatch
):
    settings = env[0].settings.model_copy(update={"context_strategy": "bm25_rrf"})
    monkeypatch.setattr("agent_py.cli.get_settings", lambda: settings)
    arguments = ["release-build", str(tmp_path / "bundle.json"), "--root", str(ROOT)]
    for name, value in release_evidence.model_dump().items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value))
        arguments += [f"--{name}", str(path)]
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["release_id"].startswith("sha256:")
    assert CliRunner().invoke(app, arguments).exit_code == 1


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("AGENT_TEST_TEMPORAL") != "1", reason="Temporal test server opt-in")
def test_temporal_release_queue_dispatch_and_cancel_after_restart(pinned):
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from agent_py.runtime import Activities, AgentWorkflow

    async def scenario():
        service, owner, _, _ = pinned
        task = service.create_task(owner, contract(), "temporal-release")
        service.stop(owner, task.id, takeover=True)
        restored = Service(service.db, service.settings.model_copy(), service.remote)
        try:
            async with await WorkflowEnvironment.start_local() as runtime:
                async with Worker(
                    runtime.client,
                    task_queue=restored.release.task_queue,
                    workflows=[AgentWorkflow],
                    activities=[Activities(restored).tick],
                ):
                    await dispatch_once(restored, runtime.client, "t1")
                    handle = runtime.client.get_workflow_handle(f"agent:t1:{task.id}")
                    await asyncio.sleep(3)
                    description = await handle.describe()
                    assert description.task_queue == restored.release.task_queue
                    restored.stop(owner, task.id)
                    result = await asyncio.wait_for(handle.result(), 30)
                    assert result["result"] == "CANCELLED"
                    assert (
                        restored.get_task(owner, task.id).contract["release_id"]
                        == restored.release.id
                    )
        finally:
            restored.telemetry.close()

    asyncio.run(scenario())
