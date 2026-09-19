import json

import httpx
import pytest
from sqlalchemy import func, select

from agent_py.adapters.enterprise import EnterpriseClient
from agent_py.collection import CollectionManifest, EvidenceCollector, EvidenceSource
from agent_py.context import ContextCompiler
from agent_py.db import Document, Grant
from agent_py.domain import DomainError, TaskContract


def source(**changes):
    return EvidenceSource.model_validate(
        {
            "name": "queue",
            "tenant": "t1",
            "project": "demo",
            "environment": "lab",
            "resource": "demo-service",
            "subjects": ["u1"],
            "provider": "jenkins_build",
            "base_url": "https://ci.example.test/",
            "token_env": "TEST_CONNECTOR_TOKEN",
            "parameters": {"job": "verify", "build": 42},
            **changes,
        }
    )


def collector(env, monkeypatch, handler, **changes):
    monkeypatch.setenv("TEST_CONNECTOR_TOKEN", "private-token")

    def factory(*a, **kw):
        return EnterpriseClient(*a, **kw, transport=httpx.MockTransport(handler))

    return EvidenceCollector(env[0], CollectionManifest(sources=[source(**changes)]), factory)


def test_scoped_collection_deduplicates_and_excludes_build_secrets(env, task, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "number": 42,
                "result": "FAILURE",
                "building": False,
                "actions": [{"parameters": [{"name": "PASSWORD", "value": "sensitive"}]}],
            },
        )

    reader = collector(env, monkeypatch, handler)
    ids = reader.collect(env[1], task.id)
    assert reader.collect(env[1], task.id) == ids
    assert len(requests) == 2 and all(r.method == "GET" for r in requests)
    with env[0].db.session("t1") as s:
        assert s.scalar(select(func.count()).select_from(Document)) == 1
        doc = s.get(Document, ids[0])
        assert doc.task_id == task.id and doc.allowed_subjects == ["u1"]
        assert "sensitive" not in doc.body and "private-token" not in doc.body
    compiler = ContextCompiler(env[0].db)
    assert compiler.compile(env[1], "demo", "lab", "FAILURE", task_id=task.id).documents
    other = env[0].create_task(
        env[1], TaskContract(kind="incident", goal="Check FAILURE", project="demo"), "other"
    )
    assert not compiler.compile(env[1], "demo", "lab", "FAILURE", task_id=other.id).documents
    assert not compiler.compile(env[1], "demo", "lab", "FAILURE").documents


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant": "t2"},
        {"environment": "production"},
        {"resource": "other"},
        {"subjects": ["other"]},
    ],
)
def test_out_of_scope_sources_never_contact_provider(env, task, monkeypatch, changes):
    def handler(_):
        pytest.fail("Unauthorized request")

    with pytest.raises(DomainError, match="authorized"):
        collector(env, monkeypatch, handler, **changes).collect(env[1], task.id)


def test_revocation_during_read_prevents_evidence_persistence(env, task, monkeypatch):
    def handler(_):
        with env[0].db.session("t1") as s:
            s.scalar(
                select(Grant).where(Grant.subject == "u1", Grant.tenant_id == "t1")
            ).revoked = True
        return httpx.Response(200, json={"result": "FAILURE"})

    with pytest.raises(DomainError, match="grant"):
        collector(env, monkeypatch, handler).collect(env[1], task.id)
    with env[0].db.session("t1") as s:
        assert s.scalar(select(func.count()).select_from(Document)) == 0


def test_deployment_projection_excludes_pod_credentials():
    from agent_py.collection import project_response

    result = project_response(
        "kubernetes_deployment",
        {
            "metadata": {"name": "queue", "annotations": {"secret": "hidden"}},
            "spec": {"replicas": 2, "template": {"password": "hidden"}},
            "status": {"readyReplicas": 1},
        },
    )
    assert "hidden" not in json.dumps(result)
    assert result["status"]["readyReplicas"] == 1


def test_worker_collects_before_inference_and_reuses_snapshot(env, task, monkeypatch, tmp_path):
    from test_investigation import response, setup

    import agent_py.collection as module

    reads, inferences = [], []

    def read(request):
        reads.append(request)
        return httpx.Response(200, json={"result": "FAILURE", "number": 42})

    reader = collector(env, monkeypatch, read)
    manifest = tmp_path / "sources.json"
    manifest.write_text(reader.manifest.model_dump_json())
    env[0].settings.collection_manifest = manifest
    monkeypatch.setattr(module, "EvidenceCollector", lambda service, manifest: reader)
    harness = setup(env, lambda request: inferences.append(request) or response())
    assert harness.tick("t1", task.id)["wait"] == "HUMAN_REVIEW"
    assert harness.tick("t1", task.id)["wait"] == "HUMAN_REVIEW"
    assert len(reads) == len(inferences) == 1
