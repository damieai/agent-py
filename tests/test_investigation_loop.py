"""Exercise durable rounds through the real budget/inference ledger and mock provider."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from agent_py.artifacts import ArtifactStore
from agent_py.db import Artifact, Document, InvestigationRound, Operation, Reservation
from agent_py.domain import DomainError, InvestigationDecision, TaskContract
from agent_py.investigation import InvestigationHarness
from agent_py.model import AnthropicGateway


def setup(env, handler):
    service, principal, _ = env
    service.settings.execution_mode = "live"
    service.settings.allow_model_api = True
    service.settings.model_id = "test-model"
    service.settings.model_api_key = SecretStr("test-only")
    task = service.create_task(
        principal,
        TaskContract(
            kind="incident",
            workflow="investigation_loop",
            goal="Investigate alpha",
            project="demo",
        ),
        "loop",
    )
    with service.db.session("t1") as s:
        for word in ("alpha", "beta", "gamma", "secret"):
            s.add(
                Document(
                    id=word,
                    tenant_id="t1",
                    project="demo",
                    source="fixture://" + word,
                    version="1",
                    body=word,
                    allowed_subjects=["other" if word == "secret" else "u1"],
                )
            )
    gateway = AnthropicGateway(service.settings, service, 1, 2, httpx.MockTransport(handler))
    return InvestigationHarness(service, gateway), task


def reply(evidence="alpha", query=None):
    return httpx.Response(
        200,
        json={
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 30, "output_tokens": 10},
            "content": [
                {
                    "type": "tool_use",
                    "name": "submit_investigation",
                    "input": {
                        "summary": "Uncertain cause",
                        "hypotheses": ["Capacity"],
                        "evidence_ids": [evidence],
                        "next_query": query,
                        "stop": query is None,
                    },
                }
            ],
        },
    )


def report(env, result):
    return json.loads(
        ArtifactStore(env[0].db, env[0].settings.artifact_root).read(env[1], result["artifact_id"])[
            1
        ]
    )


def test_three_round_limit_recovery_and_readonly(env):
    calls = []

    def handler(request):
        context = json.loads(json.loads(request.content)["messages"][0]["content"])["context"]
        calls.append(context)
        n = context["round"]
        return reply(("alpha", "beta", "gamma")[n - 1], ("beta", "gamma", "delta")[n - 1])

    harness, task = setup(env, handler)
    for n in (1, 2):
        assert harness.tick("t1", task.id) == {
            "done": False,
            "wait": "INVESTIGATION_CONTINUE",
            "round": n,
        }
    result = harness.tick("t1", task.id)
    assert harness.tick("t1", task.id) == result
    body = report(env, result)
    assert body["stop_reason"] == "ROUND_LIMIT" and len(body["rounds"]) == 3
    assert body["executed"] is False and body["requires_human_review"]
    assert [len(c["previous_decisions"]) for c in calls] == [0, 1, 2]
    assert all("secret" not in [d["id"] for d in c["documents"]] for c in calls)
    current = env[0].get_task(env[1], task.id)
    assert current.result is None and (current.spent, current.reserved) == (150, 0)
    with env[0].db.session("t1") as s:
        assert s.scalar(select(func.count()).select_from(Operation)) == 0
        assert s.scalar(select(func.count()).select_from(InvestigationRound)) == 3


@pytest.mark.parametrize(
    "query,reason",
    [
        (None, "MODEL_STOP"),
        (" investigate   ALPHA ", "REPEATED_QUERY"),
        ("alpha capacity", "NO_PROGRESS"),
        ("secret", "NO_EVIDENCE"),
    ],
)
def test_stop_conditions_freeze_without_additional_paid_calls(env, query, reason):
    calls = []
    harness, task = setup(env, lambda r: calls.append(r) or reply(query=query))
    result = harness.tick("t1", task.id)
    if result["wait"] == "INVESTIGATION_CONTINUE":
        result = harness.tick("t1", task.id)
    assert report(env, result)["stop_reason"] == reason
    with env[0].db.session("t1") as s:
        s.add(
            Document(
                id="late",
                tenant_id="t1",
                project="demo",
                source="fixture://late",
                version="1",
                body=query or "alpha",
                allowed_subjects=["u1"],
            )
        )
    assert harness.tick("t1", task.id) == result
    assert len(calls) == 1


def test_paid_decision_survives_artifact_failure_and_new_evidence(env, monkeypatch):
    calls = []
    harness, task = setup(env, lambda r: calls.append(r) or reply())
    original = ArtifactStore.put
    monkeypatch.setattr(
        ArtifactStore, "put", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(OSError):
        harness.tick("t1", task.id)
    monkeypatch.setattr(ArtifactStore, "put", original)
    with env[0].db.session("t1") as s:
        s.add(
            Document(
                id="late",
                tenant_id="t1",
                project="demo",
                source="fixture://late",
                version="1",
                body="alpha",
                allowed_subjects=["u1"],
            )
        )
    result = harness.tick("t1", task.id)
    assert report(env, result)["rounds"][0]["context"]["documents"][0]["id"] == "alpha"
    assert len(calls) == 1


@pytest.mark.parametrize("when", ["between", "during"])
def test_previous_round_evidence_revocation_blocks_next_call_or_publication(env, when):
    calls = []

    def handler(r):
        calls.append(r)
        if len(calls) == 1:
            return reply(query="beta")
        with env[0].db.session("t1") as s:
            s.get(Document, "alpha").revoked = True
        return reply("beta")

    harness, task = setup(env, handler)
    harness.tick("t1", task.id)
    if when == "between":
        with env[0].db.session("t1") as s:
            s.get(Document, "alpha").revoked = True
    with pytest.raises(DomainError, match="revoked"):
        harness.tick("t1", task.id)
    assert len(calls) == (1 if when == "between" else 2)
    with env[0].db.session("t1") as s:
        assert s.scalar(select(func.count()).select_from(Artifact)) == 0


def test_response_loss_is_not_redispatched(env):
    calls = []

    def handler(r):
        calls.append(r)
        raise httpx.ReadTimeout("lost")

    harness, task = setup(env, handler)
    with pytest.raises(httpx.ReadTimeout):
        harness.tick("t1", task.id)
    with pytest.raises(DomainError, match="persisted decision"):
        harness.tick("t1", task.id)
    assert len(calls) == 1


def test_competing_workers_only_dispatch_once(env):
    entered, release = Event(), Event()
    calls = []

    def handler(r):
        calls.append(r)
        entered.set()
        assert release.wait(10)
        return reply()

    harness, task = setup(env, handler)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(harness.tick, "t1", task.id)
        try:
            assert entered.wait(10)
            with pytest.raises(DomainError):
                pool.submit(harness.tick, "t1", task.id).result(timeout=10)
        finally:
            release.set()
        assert first.result(timeout=10)["wait"] == "HUMAN_REVIEW"
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["cancel", "takeover"])
def test_stop_between_rounds_does_not_infer(env, change):
    calls = []
    harness, task = setup(env, lambda r: calls.append(r) or reply(query="beta"))
    harness.tick("t1", task.id)
    env[0].stop(env[1], task.id, takeover=change == "takeover")
    harness.tick("t1", task.id)
    assert len(calls) == 1


def test_invalid_citation_is_settled_and_not_retried(env):
    calls = []
    harness, task = setup(env, lambda r: calls.append(r) or reply("secret"))
    with pytest.raises(DomainError, match="unknown evidence"):
        harness.tick("t1", task.id)
    with pytest.raises(DomainError, match="persisted decision"):
        harness.tick("t1", task.id)
    with env[0].db.session("t1") as s:
        row = s.scalar(select(Reservation))
        assert row.actual == 50 and row.decision is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "values",
    [
        {"stop": False},
        {"stop": True, "next_query": "beta"},
        {"stop": False, "next_query": "   "},
        {"stop": True, "next_action": {"tool": "deploy"}},
    ],
)
def test_typed_plan_rejects_ambiguous_or_write_steps(values):
    with pytest.raises(ValidationError):
        InvestigationDecision(summary="test", hypotheses=[], evidence_ids=["alpha"], **values)


def test_loop_requires_live_mode(env):
    with pytest.raises(DomainError, match="live mode"):
        env[0].create_task(
            env[1],
            TaskContract(
                kind="incident",
                workflow="investigation_loop",
                goal="Investigate alpha",
                project="demo",
            ),
            "loop",
        )


def test_collection_runs_only_before_first_frozen_round(env, monkeypatch, tmp_path):
    from agent_py.collection import EvidenceCollector

    collected = []
    manifest = tmp_path / "sources.json"
    manifest.write_text('{"sources": []}')
    env[0].settings.collection_manifest = manifest
    monkeypatch.setattr(EvidenceCollector, "collect", lambda *args: collected.append(args))
    calls = []
    harness, task = setup(
        env,
        lambda r: (
            calls.append(r)
            or reply(
                "alpha" if len(calls) == 1 else "beta",
                "beta" if len(calls) == 1 else None,
            )
        ),
    )
    harness.tick("t1", task.id)
    harness.tick("t1", task.id)
    harness.tick("t1", task.id)
    assert len(collected) == 1 and len(calls) == 2


def test_budget_exhaustion_blocks_next_round_dispatch(env):
    from agent_py.db import Task

    calls = []
    harness, task = setup(env, lambda r: calls.append(r) or reply(query="beta"))
    harness.tick("t1", task.id)
    with env[0].db.session("t1") as s:
        current = s.get(Task, task.id)
        current.contract = {**current.contract, "budget_micro_usd": 50}
    with pytest.raises(DomainError) as error:
        harness.tick("t1", task.id)
    assert "BUDGET" in error.value.code and len(calls) == 1


def test_round_foreign_key_and_unique_ordinal(env):
    from sqlalchemy.exc import IntegrityError

    _, task = setup(env, lambda _: reply())
    with pytest.raises(IntegrityError):
        with env[0].db.session("t2") as s:
            s.add(
                InvestigationRound(
                    tenant_id="t2", task_id=task.id, ordinal=1, query="cross tenant", context={}
                )
            )
    with env[0].db.session("t1") as s:
        s.add(
            InvestigationRound(
                tenant_id="t1", task_id=task.id, ordinal=1, query="first", context={}
            )
        )
    with pytest.raises(IntegrityError):
        with env[0].db.session("t1") as s:
            s.add(
                InvestigationRound(
                    tenant_id="t1", task_id=task.id, ordinal=1, query="duplicate", context={}
                )
            )
