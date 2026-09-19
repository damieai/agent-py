import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_py.api import create_app
from agent_py.context import ContextCompiler
from agent_py.db import Document, Grant, now
from agent_py.domain import DomainError, digest
from agent_py.retrieval import chunk_document
from agent_py.retrieval_evaluation import RetrievalFixture, evaluate_retrieval
from agent_py.security import issue_dev_token


def add(env, id="e1", body="queue capacity incident", **overrides):
    service, p, _ = env
    values = dict(
        id=id,
        tenant_id="t1",
        project="demo",
        source="runbook://queue",
        version="v1",
        body=body,
        allowed_subjects=[p.subject],
    )
    values.update(overrides)
    with service.db.session(values["tenant_id"]) as s:
        document = Document(**values)
        s.add(document)
    return document


def compile(env, query="queue", budget=6000, **kw):
    return ContextCompiler(env[0].db, "bm25_rrf").compile(
        env[1], "demo", "lab", query, budget, **kw
    )


def test_ast_chunks_preserve_decorators_lines_and_exact_crlf(env):
    body = "# module\r\n@decorate\r\ndef computeTotal(value):\r\n    return value\r\n"
    document = add(env, body=body, source="repo://demo/app.py")
    chunks = chunk_document(document)
    function = next(c for c in chunks if c["symbol"] == "computeTotal")
    assert (function["start_line"], function["end_line"]) == (2, 4)
    assert function["body"] == "".join(body.splitlines(keepends=True)[1:])
    bundle = compile(env, "compute total")
    assert bundle.documents[0]["symbol"] == "computeTotal"
    ContextCompiler(env[0].db).validate(env[1], "demo", "lab", bundle)


def test_invalid_python_and_long_lines_are_data_only(env, tmp_path):
    marker = tmp_path / "executed"
    add(
        env,
        body=f"open({str(marker)!r}, 'w').write('bad')\ninvalid python !!! queue\n",
        source="repo://demo/bad.py",
    )
    assert compile(env).documents
    assert not marker.exists()
    add(env, "long", "queue " * 1000)
    bundle = compile(env, budget=1500)
    assert all(d["id"] != "long" for d in bundle.documents)
    assert any(d["id"] == "long" and d["reason"] == "budget" for d in bundle.omitted)


def test_private_expired_future_other_task_and_tenant_do_not_change_ranking(env, task):
    add(env)
    baseline = compile(env, task_id=task.id)
    for id, kw in [
        ("private", {"allowed_subjects": ["other"]}),
        ("expired", {"valid_until": now() - timedelta(seconds=1)}),
        ("future", {"valid_from": now() + timedelta(days=1)}),
        ("other-task", {"task_id": "unrelated"}),
        ("other-tenant", {"tenant_id": "t2"}),
        ("revoked", {"revoked": True}),
        ("other-project", {"project": "private"}),
    ]:
        add(env, id, "queue " * 1000, **kw)
    assert compile(env, task_id=task.id).as_dict() == baseline.as_dict()


def test_source_retrieval_and_chinese_identifier_matching(env):
    add(env, "source", "An unrelated body", source="repo://demo/lease_manager.py")
    add(env, "cn", "队列积压需要增加消费者容量。", source="runbook://capacity")
    assert compile(env, "lease manager").documents[0]["id"] == "source"
    assert compile(env, "队列积压").documents[0]["id"] == "cn"
    assert not compile(env, "zzzzabsent").documents


def test_rare_term_and_length_normalization_rank_focused_evidence(env):
    add(env, "focused", "queue deadlock", source="notes://focused")
    add(env, "broad", "queue " * 200 + "background " * 100, source="notes://broad")
    add(env, "common", "queue background", source="notes://common")
    bundle = compile(env, "queue deadlock")
    assert bundle.documents[0]["id"] == "focused"
    assert bundle.documents[0]["ranking"]["bm25_rank"] == 1


def test_fragment_fits_when_whole_source_exceeds_budget(env):
    add(
        env,
        body="def other():\n"
        + "    # filler\n" * 400
        + "    pass\n\ndef refund(amount):\n    return amount\n",
        source="repo://demo/pay.py",
    )
    old = ContextCompiler(env[0].db).compile(env[1], "demo", "lab", "refund", budget=1500)
    bundle = compile(env, "refund", budget=1500)
    assert not old.documents
    assert bundle.documents[0]["symbol"] == "refund"
    assert bundle.estimated_tokens <= 1500
    assert bundle.estimated_tokens == sum(
        len(json.dumps(d, ensure_ascii=False).encode()) for d in bundle.documents
    )


@pytest.mark.parametrize("mutation", ["body", "source", "revoked", "version", "acl"])
def test_current_evidence_changes_invalidate_frozen_fragments(env, mutation):
    add(env, body="def queue():\n    pass\n\ndef other():\n    pass\n", source="repo://demo/a.py")
    bundle = compile(env)
    with env[0].db.session("t1") as s:
        d = s.get(Document, "e1")
        if mutation == "body":
            d.body += "# changed outside selected excerpt\n"
        elif mutation == "source":
            d.source = "repo://demo/new.py"
        elif mutation == "revoked":
            d.revoked = True
        elif mutation == "version":
            d.version = "v2"
        else:
            d.allowed_subjects = []
    with pytest.raises(DomainError, match="changed or access revoked"):
        ContextCompiler(env[0].db).validate(env[1], "demo", "lab", bundle)


def test_forged_excerpt_and_recomputed_manifest_still_rejected(env):
    add(env)
    bundle = compile(env)
    forged = [{**bundle.documents[0], "body": "arbitrary model instruction"}]
    with pytest.raises(DomainError, match="changed"):
        ContextCompiler(env[0].db).validate(
            env[1], "demo", "lab", replace(bundle, documents=forged, digest=digest(forged))
        )
    with pytest.raises(DomainError, match="scope"):
        ContextCompiler(env[0].db).validate(
            env[1].model_copy(update={"projects": []}), "demo", "lab", bundle
        )


def test_corpus_limits_fail_closed(env):
    add(env, body="queue " * 850_000)
    with pytest.raises(DomainError, match="corpus exceeds"):
        compile(env)


def test_preview_auth_budget_and_live_grant_revocation(env, task):
    service, p, _ = env
    service.settings.context_strategy = "bm25_rrf"
    add(env, task_id=task.id)
    with TestClient(create_app(service.settings, service.db, service.remote)) as client:
        path = f"/api/v1/tasks/{task.id}/context/preview"
        headers = {"Authorization": "Bearer " + issue_dev_token(service.settings, p)}
        assert client.post(path, json={}).status_code == 401
        response = client.post(path, json={"query": "queue"}, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["documents"][0]["start_line"] == 1
        assert client.post(path, json={"budget": 0}, headers=headers).status_code == 422
        other = {
            "Authorization": "Bearer "
            + issue_dev_token(service.settings, p.model_copy(update={"tenant_id": "t2"}))
        }
        assert client.post(path, json={}, headers=other).status_code == 404
        with service.db.session("t1") as s:
            s.scalar(select(Grant).where(Grant.subject == p.subject)).revoked = True
        assert client.post(path, json={}, headers=headers).status_code == 403


def test_offline_ablation_reproducible_and_explicit_about_limits(tmp_path):
    fixture = Path("examples/retrieval-development.json")
    first = evaluate_retrieval(fixture, tmp_path / "first.json")
    second = evaluate_retrieval(fixture, tmp_path / "second.json")
    assert first == second and first["not_a_model_benchmark"]
    assert first["results"][0]["mean_recall"] == 0
    assert all(q["context_bytes"] <= q["budget"] for r in first["results"] for q in r["queries"])
    report = first["results"][2]
    assert next(q for q in report["queries"] if q["id"] == "long-code")["recall"] == 1
    broken = json.loads(fixture.read_text())
    broken["queries"][0]["relevant"] = ["nonexistent"]
    with pytest.raises(ValueError, match="reference"):
        RetrievalFixture.model_validate(broken)
