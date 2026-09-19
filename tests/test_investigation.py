import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from agent_py.artifacts import ArtifactStore
from agent_py.db import Document, Operation
from agent_py.domain import DomainError
from agent_py.investigation import InvestigationHarness
from agent_py.model import AnthropicGateway


def setup(env, handler):
    service = env[0]
    service.settings.execution_mode = "live"
    service.settings.allow_model_api = True
    service.settings.model_id = "test-model"
    service.settings.model_api_key = SecretStr("test-only")
    gateway = AnthropicGateway(service.settings, service, 1, 2, httpx.MockTransport(handler))
    with service.db.session("t1") as s:
        s.add(
            Document(
                id="e1",
                tenant_id="t1",
                project="demo",
                source="test://fixture",
                version="1",
                body="Fix a regression in queue capacity",
                allowed_subjects=["u1"],
            )
        )
    return InvestigationHarness(service, gateway)


def response():
    return httpx.Response(
        200,
        json={
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 30, "output_tokens": 10},
            "content": [
                {
                    "type": "tool_use",
                    "name": "submit_decision",
                    "input": {
                        "summary": "Review capacity",
                        "hypotheses": ["Boundary regression"],
                        "evidence_ids": ["e1"],
                        "stop": True,
                    },
                }
            ],
        },
    )


def test_crash_after_inference_recovers_without_second_request(env, task, monkeypatch):
    calls = []
    harness = setup(env, lambda request: calls.append(request) or response())
    original = ArtifactStore.put
    monkeypatch.setattr(
        ArtifactStore, "put", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(OSError):
        harness.tick("t1", task.id)
    monkeypatch.setattr(ArtifactStore, "put", original)
    result = harness.tick("t1", task.id)
    assert harness.tick("t1", task.id)["artifact_id"] == result["artifact_id"]
    assert len(calls) == 1
    current = env[0].get_task(env[1], task.id)
    assert current.waiting_reason == "HUMAN_REVIEW" and current.result is None
    assert (current.spent, current.reserved) == (50, 0)
    with env[0].db.session("t1") as s:
        assert s.scalar(select(func.count()).select_from(Operation)) == 0


def test_response_loss_does_not_retry_paid_request(env, task):
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost response")

    harness = setup(env, fail)
    with pytest.raises(httpx.ReadTimeout):
        harness.tick("t1", task.id)
    with pytest.raises(DomainError, match="persisted decision"):
        harness.tick("t1", task.id)
    assert len(calls) == 1


def test_changed_context_cannot_reuse_inference_identity(env, task):
    harness = setup(env, lambda _: response())
    harness.tick("t1", task.id)
    with env[0].db.session("t1") as s:
        doc = s.get(Document, "e1")
        doc.body = doc.body.replace("queue", "other")
    with pytest.raises(DomainError, match="request changed"):
        harness.tick("t1", task.id)


def test_live_investigation_requires_opt_in_and_evidence(env, task):
    service = env[0]
    harness = InvestigationHarness(service)
    with pytest.raises(DomainError, match="opt-in"):
        harness.tick("t1", task.id)
    service.settings.allow_model_api = True
    assert harness.tick("t1", task.id)["wait"] == "EVIDENCE_REQUIRED"


def test_evidence_revoked_during_inference_blocks_publication(env, task):
    from agent_py.db import Artifact

    def handler(_):
        with env[0].db.session("t1") as s:
            s.get(Document, "e1").revoked = True
        return response()

    harness = setup(env, handler)
    with pytest.raises(DomainError, match="revoked"):
        harness.tick("t1", task.id)
    with env[0].db.session("t1") as s:
        assert s.scalar(select(func.count()).select_from(Artifact)) == 0


def test_runtime_selects_readonly_harness(env, task):
    import asyncio

    from agent_py.runtime import Activities

    service = env[0]
    service.settings.execution_mode = "live"
    service.settings.allow_model_api = True
    activities = Activities(service)
    assert isinstance(activities.harness, InvestigationHarness)
    result = asyncio.run(activities.tick({"tenant": "t1", "task_id": task.id}))
    assert result == {"done": False, "wait": "EVIDENCE_REQUIRED"}
