import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_investigation_loop import reply, setup
from typer.testing import CliRunner

from agent_py.api import create_app
from agent_py.artifacts import ArtifactStore
from agent_py.db import Document, Grant, InvestigationRound, Reservation, now
from agent_py.domain import DomainError
from agent_py.investigation_status import investigation_details
from agent_py.security import issue_dev_token


def test_readonly_status_tracks_rounds_cost_and_stop(env):
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
    service, principal, _ = env
    assert investigation_details(service, principal, task.id)["rounds"] == []
    harness.tick("t1", task.id)
    data = investigation_details(service, principal, task.id)
    assert data["stop_reason"] is None
    assert data["rounds"][0]["state"] == "DECIDED"
    assert data["rounds"][0]["spent_micro_usd"] == 50
    assert data["rounds"][0]["reserved_micro_usd"] == 0
    assert "body" not in data["rounds"][0]["evidence"][0]
    harness.tick("t1", task.id)
    service.settings.allow_model_api = False  # Reading needs no paid-inference opt-in.
    for _ in range(2):
        data = investigation_details(service, principal, task.id)
        assert data["stop_reason"] == "MODEL_STOP"
        assert data["waiting_reason"] == "HUMAN_REVIEW"
        assert len(data["rounds"]) == 2 and data["executed"] is False
    assert len(calls) == 2
    with service.db.session("t1") as s:
        assert s.scalar(select(func.count()).select_from(InvestigationRound)) == 2
        assert s.scalar(select(func.count()).select_from(Reservation)) == 2


@pytest.mark.parametrize(
    "query,reason",
    [
        ("ALPHA CAPACITY", "NO_PROGRESS"),
        ("unmatched", "NO_EVIDENCE"),
        (" investigate ALPHA ", "REPEATED_QUERY"),
    ],
)
def test_status_stop_matches_worker(env, query, reason):
    harness, task = setup(env, lambda _: reply(query=query))
    harness.tick("t1", task.id)
    harness.tick("t1", task.id)
    data = investigation_details(env[0], env[1], task.id)
    assert data["stop_reason"] == reason


def test_round_limit_visible(env):
    calls = []

    def handler(r):
        calls.append(r)
        n = len(calls) - 1
        return reply(("alpha", "beta", "gamma")[n], ("beta", "gamma", "delta")[n])

    harness, task = setup(env, handler)
    for _ in range(3):
        harness.tick("t1", task.id)
    assert investigation_details(env[0], env[1], task.id)["stop_reason"] == "ROUND_LIMIT"


def test_unknown_response_status_does_not_retry(env):
    def handler(_):
        raise httpx.ReadTimeout("lost")

    harness, task = setup(env, handler)
    with pytest.raises(httpx.ReadTimeout):
        harness.tick("t1", task.id)
    row = investigation_details(env[0], env[1], task.id)["rounds"][0]
    assert row["state"] == "RESPONSE_UNKNOWN" and row["spent_micro_usd"] > 0
    assert row["decision"] is None


@pytest.mark.parametrize("dispatched,expected", [(False, "READY"), (True, "IN_FLIGHT")])
def test_reserved_and_dispatched_are_distinct(env, dispatched, expected, monkeypatch):
    harness, task = setup(env, lambda _: reply())

    def pause(*args, **kwargs):
        raise RuntimeError("pause before inference")

    monkeypatch.setattr(harness.gateway, "investigate", pause)
    with pytest.raises(RuntimeError):
        harness.tick("t1", task.id)
    reservation = env[0].reserve("t1", task.id, "investigation-loop:v1:1", 100)
    if dispatched:
        env[0].claim_inference("t1", reservation.id)
    row = investigation_details(env[0], env[1], task.id)["rounds"][0]
    assert row["state"] == expected and row["reserved_micro_usd"] == 100


@pytest.mark.parametrize("mutation", ["revoke", "body", "version", "expire", "acl"])
def test_changed_evidence_blocks_status_and_report_download(env, mutation):
    harness, task = setup(env, lambda _: reply())
    result = harness.tick("t1", task.id)
    with env[0].db.session("t1") as s:
        doc = s.get(Document, "alpha")
        if mutation == "revoke":
            doc.revoked = True
        elif mutation == "body":
            doc.body = "changed"
        elif mutation == "version":
            doc.version = "2"
        elif mutation == "expire":
            doc.valid_until = now() - timedelta(seconds=1)
        elif mutation == "acl":
            doc.allowed_subjects = ["reviewer"]
    with pytest.raises(DomainError, match="revoked"):
        investigation_details(env[0], env[1], task.id)
    with pytest.raises(DomainError, match="revoked"):
        ArtifactStore(env[0].db, env[0].settings.artifact_root).read(env[1], result["artifact_id"])


def test_reader_must_have_evidence_acl_even_with_operator_role(env):
    harness, task = setup(env, lambda _: reply())
    result = harness.tick("t1", task.id)
    with pytest.raises(DomainError, match="revoked"):
        investigation_details(env[0], env[2], task.id)
    with pytest.raises(DomainError, match="revoked"):
        ArtifactStore(env[0].db, env[0].settings.artifact_root).read(env[2], result["artifact_id"])
    with env[0].db.session("t1") as s:
        s.get(Document, "alpha").allowed_subjects = ["u1", "reviewer"]
    assert investigation_details(env[0], env[2], task.id)["rounds"][0]["decision"]


def test_status_api_auth_cache_and_no_partial_disclosure(env):
    harness, task = setup(env, lambda _: reply())
    harness.tick("t1", task.id)
    service, principal, _ = env
    client = TestClient(create_app(service.settings, service.db, service.remote))
    url = f"/api/v1/tasks/{task.id}/investigation"
    headers = {"Authorization": "Bearer " + issue_dev_token(service.settings, principal)}
    assert client.get(url).status_code == 401
    response = client.get(url, headers=headers)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.json()["investigation"]["stop_reason"] == "MODEL_STOP"
    other = principal.model_copy(update={"tenant_id": "t2"})
    assert (
        client.get(
            url, headers={"Authorization": "Bearer " + issue_dev_token(service.settings, other)}
        ).status_code
        == 404
    )
    with service.db.session("t1") as s:
        s.get(Document, "alpha").revoked = True
    response = client.get(url, headers=headers)
    assert response.status_code == 409 and "Uncertain cause" not in response.text
    with service.db.session("t1") as s:
        s.scalar(select(Grant).where(Grant.subject == "u1")).revoked = True
    assert client.get(url, headers=headers).status_code == 403


def test_status_non_loop_task_is_empty(env, task):
    assert investigation_details(env[0], env[1], task.id) is None


def test_cli_creation_idempotency_and_status(env, monkeypatch):
    from agent_py import cli

    service, principal, _ = env
    service.settings.execution_mode = "live"
    monkeypatch.setattr(cli, "build_service", lambda _: service)
    monkeypatch.setattr(cli, "principal", lambda: principal)
    runner = CliRunner()
    args = ["investigation-create", "Investigate alpha", "--request-key", "cli-loop"]
    first = runner.invoke(cli.app, args)
    assert first.exit_code == 0, first.output
    assert runner.invoke(cli.app, args).output == first.output
    task = service.get_task(principal, first.output.strip())
    assert task.contract["workflow"] == "investigation_loop" and task.contract["kind"] == "incident"
    status = runner.invoke(cli.app, ["investigation-status", task.id])
    assert status.exit_code == 0 and json.loads(status.output)["rounds"] == []
    assert runner.invoke(cli.app, args + ["--kind", "invalid"]).exit_code != 0
