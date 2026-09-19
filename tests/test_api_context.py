import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agent_py.api import create_app
from agent_py.artifacts import ArtifactStore
from agent_py.context import ContextCompiler
from agent_py.db import Document, now
from agent_py.domain import DomainError
from agent_py.harness import SimulationHarness
from agent_py.replay import ReplayExecutor, export_recording
from agent_py.security import issue_dev_token


def test_api_authenticated_and_idempotent(env):
    service, p, _ = env
    client = TestClient(create_app(service.settings, service.db, service.remote))
    body = {"kind": "repair", "goal": "Fix the regression", "project": "demo"}
    assert client.post("/api/v1/tasks", json=body).status_code == 401
    headers = {
        "Authorization": "Bearer " + issue_dev_token(service.settings, p),
        "Idempotency-Key": "api-create",
    }
    first = client.post("/api/v1/tasks", json=body, headers=headers)
    assert first.status_code == 202, first.text
    second = client.post("/api/v1/tasks", json=body, headers=headers)
    assert second.json()["id"] == first.json()["id"]
    assert (
        client.get("/api/v1/tasks", headers=headers).json()["items"][0]["id"] == first.json()["id"]
    )


def test_expired_token_and_wrong_issuer(env):
    service, p, _ = env
    client = TestClient(create_app(service.settings, service.db, service.remote))
    token = issue_dev_token(service.settings, p, seconds=-1)
    assert (
        client.get("/api/v1/tasks", headers={"Authorization": "Bearer " + token}).status_code == 401
    )


def test_context_acl_time_and_revocation(env):
    service, p, _ = env
    with service.db.session("t1") as s:
        allowed = Document(
            tenant_id="t1",
            project="demo",
            source="runbook://queue",
            version="1",
            body="queue latency worker capacity",
            allowed_subjects=[p.subject],
            valid_from=now() - timedelta(days=1),
        )
        s.add(allowed)
        s.add(
            Document(
                tenant_id="t1",
                project="demo",
                source="private",
                version="1",
                body="queue secret",
                allowed_subjects=["someone-else"],
            )
        )
        s.add(
            Document(
                tenant_id="t1",
                project="demo",
                source="future",
                version="1",
                body="queue future",
                allowed_subjects=[p.subject],
                valid_from=now() + timedelta(days=1),
            )
        )
        s.flush()
        document_id = allowed.id
    compiler = ContextCompiler(service.db)
    bundle = compiler.compile(p, "demo", "lab", "queue latency")
    assert [d["id"] for d in bundle.documents] == [document_id]
    with service.db.session("t1") as s:
        s.get(Document, document_id).revoked = True
    with pytest.raises(DomainError, match="revoked"):
        compiler.validate(p, "demo", "lab", bundle)
    assert not compiler.compile(p, "demo", "lab", "queue").documents


def test_artifact_auth_and_integrity(env, task):
    service, p, _ = env
    store = ArtifactStore(service.db, service.settings.artifact_root)
    a = store.put("t1", task.id, "test", b'{"verified": true}')
    assert json.loads(store.read(p, a.id)[1])["verified"]
    with pytest.raises(DomainError):
        store.read(p.model_copy(update={"tenant_id": "t2"}), a.id)
    (store.root / a.storage_key).write_bytes(b"tampered")
    with pytest.raises(DomainError, match="checksum"):
        store.read(p, a.id)


@pytest.mark.parametrize("kind", ["repair", "incident"])
def test_both_harness_tracks_and_strict_replay(env, kind):
    from agent_py.db import Approval
    from agent_py.domain import TaskContract

    service, p, reviewer = env
    task = service.create_task(
        p,
        TaskContract(kind=kind, goal="Complete isolated demonstration", project="demo"),
        "harness-" + kind,
    )
    harness = SimulationHarness(service)
    for _ in range(20):
        result = harness.tick("t1", task.id)
        if result.get("wait") == "APPROVAL":
            with service.db.session("t1") as s:
                a = s.get(Approval, result["approval_id"])
            service.decide(reviewer, a.id, "approve", a.payload_digest)
        if result.get("done"):
            break
    assert result == {"done": True, "result": "SUCCESS"}
    recording = export_recording(service, p, task.id)
    replay = ReplayExecutor(recording)
    op = recording["body"]["operations"][0]
    assert replay.execute("unused", op["id"], **op["request"])["confirmed"]
    with pytest.raises(DomainError, match="matching"):
        replay.execute("unused", "missing", **op["request"])


def test_revoked_scope_blocks_api_existing_token(env, task):
    from sqlalchemy import select

    from agent_py.db import Grant

    service, p, _ = env
    client = TestClient(create_app(service.settings, service.db, service.remote))
    headers = {"Authorization": "Bearer " + issue_dev_token(service.settings, p)}
    assert client.get(f"/api/v1/tasks/{task.id}", headers=headers).status_code == 200
    with service.db.session("t1") as s:
        s.scalar(
            select(Grant).where(Grant.tenant_id == "t1", Grant.subject == p.subject)
        ).revoked = True
    assert client.get(f"/api/v1/tasks/{task.id}", headers=headers).status_code == 403
