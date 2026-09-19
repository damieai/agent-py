import hashlib
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from agent_py.artifacts import ArtifactStore
from agent_py.db import Grant, Operation, RepairAttempt, RepairRun, now
from agent_py.domain import DomainError, TaskContract
from agent_py.model import AnthropicGateway
from agent_py.repair import RepairHarness, repair_details
from agent_py.sandbox import SandboxResult
from agent_py.verification import VerificationRunner


@pytest.fixture
def repair_env(env, tmp_path):
    service, principal, _ = env
    settings = service.settings
    settings.execution_mode = "live"
    settings.allow_model_api = settings.allow_candidate_execution = True
    settings.model_id = "test-model"
    settings.model_api_key = SecretStr("not-a-real-key")
    settings.model_input_micro_per_token, settings.model_output_micro_per_token = 1, 2
    settings.sandbox_root = tmp_path / "sandbox"
    settings.sandbox_image = "python@sha256:" + "a" * 64
    settings.sandbox_oracle = tmp_path / "oracle"
    settings.sandbox_oracle.mkdir()
    (settings.sandbox_oracle / "test_app.py").write_text("def test_oracle(): pass\n")
    source = tmp_path / "repo"
    (source / "src").mkdir(parents=True)
    (source / "src/app.py").write_text("def value():\n    return 1\n")
    settings.repair_manifest = tmp_path / "repair.json"
    settings.repair_manifest.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "tenant": "t1",
                        "project": "demo",
                        "environment": "lab",
                        "resource": "demo-service",
                        "subjects": ["u1"],
                        "source_root": str(source),
                        "allow_model_export": True,
                        "max_attempts": 2,
                    }
                ]
            }
        )
    )
    task = service.create_task(
        principal,
        TaskContract(
            kind="repair",
            workflow="repair_candidate",
            goal="Fix boundary regression",
            project="demo",
        ),
        "repair-task",
    )
    model_calls, containers = [], []
    behavior = {"fail_first": False, "always_fail": False, "lose_response": False}

    def model(request):
        model_calls.append(request)
        if behavior["lose_response"]:
            raise httpx.ReadTimeout("response lost")
        context = json.loads(json.loads(request.content)["messages"][0]["content"])["context"]
        original = context["sources"]["src/app.py"]
        content = (
            original
            if behavior["always_fail"] or (behavior["fail_first"] and len(model_calls) == 1)
            else original.replace("return 1", "return 2")
        )
        proposal = {
            "summary": "Fix the boundary",
            "evidence_ids": ["source:src/app.py"],
            "edits": [
                {
                    "path": "src/app.py",
                    "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
                    "content": content,
                }
            ],
        }
        if behavior.get("bad_path"):
            proposal["edits"][0]["path"] = "tests/test_app.py"
        return httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 20, "output_tokens": 10},
                "content": [{"type": "tool_use", "name": "submit_patch", "input": proposal}],
            },
        )

    class Sandbox:
        def __init__(self, image, root):
            pass

        def verify(self, workspace, *, oracle, timeout_seconds):
            # Inspect trusted fixture bytes, never import/execute generated candidate code.
            body = (workspace / "src/app.py").read_text()
            containers.append(body)
            if behavior.get("sandbox_crash"):
                raise OSError("daemon unavailable")
            return SandboxResult(0 if "return 2" in body else 1, "bounded fixture output")

    gateway = AnthropicGateway(settings, service, 1, 2, httpx.MockTransport(model))
    harness = RepairHarness(service, gateway, VerificationRunner(service, Sandbox))
    return service, principal, task, harness, behavior, model_calls, containers, source


def advance_to_patch(fixture):
    service, principal, task, harness, *_ = fixture
    assert harness.tick("t1", task.id)["phase"] == "SNAPSHOT_SAVED"
    assert harness.tick("t1", task.id)["phase"] == "ATTEMPT_CREATED"
    assert harness.tick("t1", task.id)["phase"] == "PATCH_GENERATED"
    return repair_details(service, principal, task.id)["attempts"][0]


def test_complete_candidate_review_loop_does_not_execute_business_actions(repair_env):
    service, principal, task, harness, _, model, containers, source = repair_env
    advance_to_patch(repair_env)
    assert harness.tick("t1", task.id)["wait"] == "CANDIDATE_READY_FOR_REVIEW"
    assert harness.tick("t1", task.id)["wait"] == "CANDIDATE_READY_FOR_REVIEW"
    details = repair_details(service, principal, task.id)
    assert details["attempts"][0]["outcome"] == "REGRESSION_FIXED"
    assert len(model) == 1 and len(containers) == 2
    assert "return 1" in (source / "src/app.py").read_text()
    assert service.get_task(principal, task.id).result is None
    assert service.get_task(principal, task.id).spent == 40
    with service.db.session("t1") as s:
        assert s.scalar(select(func.count()).select_from(Operation)) == 0


def test_failed_candidate_gets_bounded_feedback_and_second_attempt(repair_env):
    service, principal, task, harness, behavior, model, containers, _ = repair_env
    behavior["fail_first"] = True
    for _ in range(10):
        result = harness.tick("t1", task.id)
        if result.get("wait") == "CANDIDATE_READY_FOR_REVIEW":
            break
    assert repair_details(service, principal, task.id)["state"] == "CANDIDATE_READY_FOR_REVIEW"
    assert len(model) == 2 and len(containers) == 4
    context = json.loads(json.loads(model[1].content)["messages"][0]["content"])["context"]
    assert context["feedback"] == [{"attempt": 1, "outcome": "CANDIDATE_FAILED"}]
    assert "bounded fixture output" not in json.dumps(context)
    assert "test_oracle" not in json.dumps(context)


def test_attempt_limit_stops_without_more_model_calls(repair_env):
    service, principal, task, harness, behavior, model, containers, _ = repair_env
    behavior["always_fail"] = True
    for _ in range(12):
        harness.tick("t1", task.id)
    assert repair_details(service, principal, task.id)["state"] == "REPAIR_EXHAUSTED"
    assert len(model) == 2 and len(containers) == 4


def test_generation_result_recovers_after_patch_artifact_failure(repair_env, monkeypatch):
    _, _, task, harness, _, model, _, _ = repair_env
    harness.tick("t1", task.id)
    harness.tick("t1", task.id)
    original = harness.store.put

    def fail(*args, **kwargs):
        if args[2] == "repair-patch":
            raise OSError("disk unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(harness.store, "put", fail)
    with pytest.raises(OSError):
        harness.tick("t1", task.id)
    monkeypatch.setattr(harness.store, "put", original)
    assert harness.tick("t1", task.id)["phase"] == "PATCH_GENERATED"
    assert len(model) == 1


def test_saved_verification_report_recovers_without_restarting_container(repair_env, monkeypatch):
    _, _, task, harness, _, _, containers, _ = repair_env
    advance_to_patch(repair_env)
    original = harness._verified
    monkeypatch.setattr(
        harness, "_verified", lambda *args: (_ for _ in ()).throw(SystemExit("worker died"))
    )
    with pytest.raises(SystemExit):
        harness.tick("t1", task.id)
    monkeypatch.setattr(harness, "_verified", original)
    assert harness.tick("t1", task.id)["wait"] == "CANDIDATE_READY_FOR_REVIEW"
    assert len(containers) == 2


def test_interrupted_verification_needs_explicit_retry_and_no_new_inference(repair_env):
    service, principal, task, harness, behavior, model, containers, _ = repair_env
    attempt = advance_to_patch(repair_env)
    behavior["sandbox_crash"] = True
    with pytest.raises(OSError):
        harness.tick("t1", task.id)
    assert harness.tick("t1", task.id)["wait"] == "VERIFICATION_INTERRUPTED"
    assert len(containers) == 1
    harness.retry_verification(principal, task.id, attempt["id"])
    behavior["sandbox_crash"] = False
    assert harness.tick("t1", task.id)["wait"] == "CANDIDATE_READY_FOR_REVIEW"
    assert len(model) == 1 and len(containers) == 3
    assert repair_details(service, principal, task.id)["attempts"][0]["verification_count"] == 2


def test_lost_model_response_is_not_retried(repair_env):
    _, _, task, harness, behavior, model, containers, _ = repair_env
    behavior["lose_response"] = True
    harness.tick("t1", task.id)
    harness.tick("t1", task.id)
    with pytest.raises(httpx.ReadTimeout):
        harness.tick("t1", task.id)
    with pytest.raises(DomainError, match="persisted decision"):
        harness.tick("t1", task.id)
    assert len(model) == 1 and not containers


@pytest.mark.parametrize("change", ["source", "oracle", "model", "image"])
def test_input_drift_blocks_candidate_execution(repair_env, change):
    service, _, task, harness, _, _, containers, source = repair_env
    advance_to_patch(repair_env)
    if change == "source":
        (source / "src/app.py").write_text("changed")
    elif change == "oracle":
        (service.settings.sandbox_oracle / "test_app.py").write_text("changed")
    elif change == "image":
        service.settings.sandbox_image = "python@sha256:" + "b" * 64
    else:
        service.settings.model_id = "different"
    with pytest.raises(DomainError, match="changed"):
        harness.tick("t1", task.id)
    assert not containers


def test_cancel_and_revocation_prevent_more_work(repair_env):
    service, principal, task, harness, _, model, containers, _ = repair_env
    advance_to_patch(repair_env)
    with service.db.session("t1") as s:
        s.scalar(
            select(Grant).where(Grant.tenant_id == "t1", Grant.subject == principal.subject)
        ).revoked = True
    with pytest.raises(DomainError, match="grant"):
        harness.tick("t1", task.id)
    assert not containers and len(model) == 1
    with service.db.session("t1") as s:
        s.scalar(
            select(Grant).where(Grant.tenant_id == "t1", Grant.subject == principal.subject)
        ).revoked = False
    service.stop(principal, task.id)
    assert harness.tick("t1", task.id)["result"] == "CANCELLED"


def test_invalid_model_patch_never_reaches_sandbox(repair_env):
    _, _, task, harness, behavior, model, containers, _ = repair_env
    behavior["bad_path"] = True
    harness.tick("t1", task.id)
    harness.tick("t1", task.id)
    with pytest.raises(DomainError, match="source files"):
        harness.tick("t1", task.id)
    with pytest.raises(DomainError, match="persisted decision"):
        harness.tick("t1", task.id)
    assert len(model) == 1 and not containers


def test_verification_lease_expiry_is_blocked_not_reexecuted(repair_env):
    service, _, task, harness, _, _, containers, _ = repair_env
    attempt = advance_to_patch(repair_env)
    with service.db.session("t1") as s:
        current = s.get(RepairAttempt, attempt["id"])
        current.state, current.verification_token = "VERIFYING", "old-token"
        current.verification_started_at = now() - timedelta(minutes=10)
        current.verification_count = 1
    harness.tick("t1", task.id)
    assert harness.tick("t1", task.id)["wait"] == "VERIFICATION_INTERRUPTED"
    assert not containers


def test_receipt_from_superseded_verification_cannot_complete_attempt(repair_env):
    service, principal, task, harness, _, _, _, _ = repair_env
    attempt = advance_to_patch(repair_env)
    with service.db.session("t1") as s:
        current = s.get(RepairAttempt, attempt["id"])
        run = s.get(RepairRun, current.run_id)
        current.state = "VERIFYING"
        current.verification_token = "new-token"
    with pytest.raises(DomainError, match="match"):
        fake = ArtifactStore(service.db, service.settings.artifact_root).put(
            "t1",
            task.id,
            "candidate-verification",
            json.dumps({"verification_id": "old-token", "outcome": "REGRESSION_FIXED"}).encode(),
        )
        harness._verified(
            principal,
            task.id,
            run,
            current,
            {"artifact_id": fake.id, "outcome": "REGRESSION_FIXED"},
        )


def test_repair_api_scope_patch_preview_and_retry_permissions(repair_env):
    from fastapi.testclient import TestClient

    from agent_py.api import create_app
    from agent_py.security import issue_dev_token

    service, principal, task, harness, behavior, _, _, _ = repair_env
    attempt = advance_to_patch(repair_env)
    client = TestClient(create_app(service.settings, service.db, service.remote))
    headers = {"Authorization": "Bearer " + issue_dev_token(service.settings, principal)}
    response = client.get(f"/api/v1/tasks/{task.id}/repair", headers=headers)
    assert (
        response.status_code == 200
        and response.json()["repair"]["attempts"][0]["id"] == attempt["id"]
    )
    preview = client.get(
        f"/api/v1/tasks/{task.id}/repair/attempts/{attempt['id']}/patch", headers=headers
    )
    assert preview.status_code == 200 and "+    return 2" in preview.json()["diff"]
    assert preview.headers["cache-control"] == "no-store"
    other = principal.model_copy(update={"tenant_id": "t2"})
    denied = {"Authorization": "Bearer " + issue_dev_token(service.settings, other)}
    assert client.get(f"/api/v1/tasks/{task.id}/repair", headers=denied).status_code == 404
    behavior["sandbox_crash"] = True
    with pytest.raises(OSError):
        harness.tick("t1", task.id)
    developer = principal.model_copy(update={"roles": ["developer"]})
    developer_headers = {"Authorization": "Bearer " + issue_dev_token(service.settings, developer)}
    url = f"/api/v1/tasks/{task.id}/repair/retry-verification"
    assert (
        client.post(url, headers=developer_headers, json={"attempt_id": attempt["id"]}).status_code
        == 403
    )
    assert client.post(url, headers=headers, json={"attempt_id": attempt["id"]}).status_code == 202
    assert client.post(url, headers=headers, json={"attempt_id": attempt["id"]}).status_code == 409


def test_repair_requires_two_opt_ins_and_repository_export_permission(repair_env):
    service, _, task, harness, _, model, containers, _ = repair_env
    service.settings.allow_candidate_execution = False
    with pytest.raises(DomainError, match="opt-in"):
        harness.tick("t1", task.id)
    service.settings.allow_candidate_execution = True
    content = json.loads(service.settings.repair_manifest.read_text())
    content["repositories"][0]["allow_model_export"] = False
    service.settings.repair_manifest.write_text(json.dumps(content))
    with pytest.raises(DomainError, match="export-authorized"):
        harness.tick("t1", task.id)
    assert not model and not containers


def test_verification_retries_have_separate_hard_limit(repair_env):
    _, principal, task, harness, behavior, model, containers, _ = repair_env
    attempt = advance_to_patch(repair_env)
    behavior["sandbox_crash"] = True
    for i in range(3):
        with pytest.raises(OSError):
            harness.tick("t1", task.id)
        if i < 2:
            harness.retry_verification(principal, task.id, attempt["id"])
    with pytest.raises(DomainError, match="at most three"):
        harness.retry_verification(principal, task.id, attempt["id"])
    assert len(model) == 1 and len(containers) == 3


def test_concurrent_generation_does_not_duplicate_paid_request(repair_env):
    from concurrent.futures import ThreadPoolExecutor

    _, _, task, harness, _, model, _, _ = repair_env
    harness.tick("t1", task.id)
    harness.tick("t1", task.id)

    def tick():
        try:
            return harness.tick("t1", task.id)
        except DomainError as exc:
            assert exc.code in {"INFERENCE_ALREADY_DISPATCHED", "MODEL_CALL_ALREADY_SETTLED"}
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: tick(), range(2)))
    assert len(model) == 1


def test_budget_refusal_happens_before_provider_request(repair_env):
    from agent_py.db import Task

    service, _, task, harness, _, model, _, _ = repair_env
    with service.db.session("t1") as s:
        current = s.get(Task, task.id)
        current.contract = {**current.contract, "budget_micro_usd": 1}
    harness.tick("t1", task.id)
    harness.tick("t1", task.id)
    with pytest.raises(DomainError, match="budget"):
        harness.tick("t1", task.id)
    assert not model


def test_crlf_source_hash_is_preserved(repair_env):
    _, _, task, harness, _, model, _, source = repair_env
    (source / "src/app.py").write_bytes(b"def value():\r\n    return 1\r\n")
    advance_to_patch(repair_env)
    assert len(model) == 1
    assert harness.tick("t1", task.id)["wait"] == "CANDIDATE_READY_FOR_REVIEW"


@pytest.mark.integration
@pytest.mark.skipif(
    __import__("os").getenv("AGENT_TEST_TEMPORAL") != "1",
    reason="Temporal repair integration opt-in",
)
def test_temporal_advances_entire_repair_loop_and_cancels_review(repair_env):
    import asyncio

    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from agent_py.runtime import Activities, AgentWorkflow

    service, principal, task, harness, _, model, containers, _ = repair_env

    async def scenario():
        async with await WorkflowEnvironment.start_local() as runtime:
            activities = Activities(service)
            activities.harness = harness
            async with Worker(
                runtime.client,
                task_queue="repair-test",
                workflows=[AgentWorkflow],
                activities=[activities.tick],
            ):
                handle = await runtime.client.start_workflow(
                    AgentWorkflow.run,
                    {"tenant": "t1", "task_id": task.id},
                    id=task.id,
                    task_queue="repair-test",
                )
                for _ in range(60):
                    current = repair_details(service, principal, task.id)
                    if current and current["state"] == "CANDIDATE_READY_FOR_REVIEW":
                        break
                    await asyncio.sleep(1)
                else:
                    pytest.fail("Repair did not reach review")
                assert len(model) == 1 and len(containers) == 2
                service.stop(principal, task.id)
                result = await asyncio.wait_for(handle.result(), timeout=25)
                assert result["result"] == "CANCELLED"

    asyncio.run(scenario())


def test_concurrent_verification_claim_launches_only_one_pair(repair_env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    _, _, task, harness, _, _, containers, _ = repair_env
    advance_to_patch(repair_env)
    started, release = Event(), Event()
    original = harness.verifier.run

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    harness.verifier.run = delayed
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(harness.tick, "t1", task.id)
        assert started.wait(5)
        try:
            assert harness.tick("t1", task.id)["wait"] == "VERIFICATION_PENDING"
        finally:
            release.set()
        assert first.result()["wait"] == "CANDIDATE_READY_FOR_REVIEW"
    assert len(containers) == 2


def test_run_finalization_recovers_after_attempt_commit(repair_env, monkeypatch):
    _, _, task, harness, _, model, containers, _ = repair_env
    advance_to_patch(repair_env)
    original = harness._end
    monkeypatch.setattr(
        harness, "_end", lambda *args: (_ for _ in ()).throw(SystemExit("worker died"))
    )
    with pytest.raises(SystemExit):
        harness.tick("t1", task.id)
    monkeypatch.setattr(harness, "_end", original)
    assert harness.tick("t1", task.id)["wait"] == "CANDIDATE_READY_FOR_REVIEW"
    assert len(model) == 1 and len(containers) == 2


def test_nonowner_developer_cannot_read_source_artifacts(repair_env):
    service, principal, task, harness, *_ = repair_env
    attempt = advance_to_patch(repair_env)
    peer = principal.model_copy(update={"subject": "peer", "roles": ["developer"]})
    with service.db.session("t1") as s:
        s.add(Grant(tenant_id="t1", subject="peer", project="demo", environment="lab"))
    with pytest.raises(DomainError, match="ownership"):
        repair_details(service, peer, task.id)
    with pytest.raises(DomainError, match="ownership"):
        harness.store.read(peer, attempt["patch_artifact_id"])


def test_observed_source_invalidation_cannot_be_undone_by_late_completion(repair_env):
    service, principal, task, harness, _, _, containers, source = repair_env
    advance_to_patch(repair_env)
    original = (source / "src/app.py").read_bytes()
    (source / "src/app.py").write_text("changed")
    with pytest.raises(DomainError, match="changed"):
        harness.tick("t1", task.id)
    details = repair_details(service, principal, task.id)
    assert details["state"] == "REPAIR_INPUT_CHANGED"
    (source / "src/app.py").write_bytes(original)
    assert harness.tick("t1", task.id)["wait"] == "REPAIR_INPUT_CHANGED"
    assert (
        harness._end(principal, task.id, details["id"], "CANDIDATE_READY_FOR_REVIEW")["wait"]
        == "REPAIR_INPUT_CHANGED"
    )
    assert not containers
