import copy
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_py.api import create_app
from agent_py.audit import check_recording, export_audit
from agent_py.db import Approval, Grant, TaskEvent
from agent_py.domain import DomainError, digest
from agent_py.event_feed import event_page
from agent_py.harness import SimulationHarness
from agent_py.replay import ReplayExecutor
from agent_py.security import issue_dev_token


def complete(env, task, reject=False):
    service, p, reviewer = env
    harness = SimulationHarness(service)
    for _ in range(25):
        outcome = harness.tick(p.tenant_id, task.id)
        if outcome.get("wait") == "APPROVAL":
            with service.db.session(p.tenant_id) as s:
                approval = s.get(Approval, outcome["approval_id"])
            service.decide(
                reviewer, approval.id, "reject" if reject else "approve", approval.payload_digest
            )
        if outcome.get("done"):
            break
    assert outcome.get("done")
    return export_audit(service, p, task.id)


def reseal(recording):
    recording["digest"] = digest(recording["body"])
    return recording


def test_ordered_replay_checks_scope_exact_types_and_completion(env, task):
    recording = complete(env, task)
    report = check_recording(recording)
    assert report["fully_confirmed_dispatches"] and not report["remote_state_verified"]
    replay = ReplayExecutor(recording)
    first, second = recording["body"]["operations"][:2]
    with pytest.raises(DomainError, match="tenant"):
        replay.execute("t2", first["id"], **first["request"])
    with pytest.raises(DomainError, match="next"):
        replay.execute("t1", second["id"], **second["request"])
    with pytest.raises(DomainError, match="unconsumed"):
        replay.assert_consumed()
    # The recorder cannot be mutated through the caller's original dictionary.
    original = copy.deepcopy(first["request"])
    first["request"]["parameters"]["unexpected"] = True
    assert replay.execute("t1", first["id"], **original)["confirmed"]
    with pytest.raises(DomainError, match="next"):
        replay.execute("t1", first["id"], **original)
    for op in recording["body"]["operations"][1:]:
        replay.execute("t1", op["id"], **op["request"])
    replay.assert_consumed()


@pytest.mark.parametrize(
    "damage",
    [
        "checksum",
        "gap",
        "duplicate",
        "approval",
        "confirmation",
        "attempts",
        "request",
        "status",
        "artifact",
    ],
)
def test_resealed_inconsistent_recording_is_rejected(env, task, damage):
    recording = complete(env, task)
    body = recording["body"]
    if damage == "checksum":
        recording["digest"] = "0" * 64
    elif damage == "gap":
        body["events"].pop(1)
    elif damage == "duplicate":
        body["operations"].append(body["operations"][0])
    elif damage == "approval":
        body["approvals"][0]["payload_digest"] = "bad"
    elif damage == "confirmation":
        body["operations"][0]["result"]["confirmed"] = 1
    elif damage == "attempts":
        body["operations"][0]["attempts"] = 2
    elif damage == "request":
        body["operations"][0]["request"]["parameters"]["injected"] = True
    elif damage == "status":
        body["operations"][0]["status"] = "PENDING"
    else:
        body["artifacts"] = []
    if damage != "checksum":
        reseal(recording)
    with pytest.raises(DomainError):
        check_recording(recording)


def test_pending_and_unknown_export_without_fabricated_confirmation(env, task):
    service, p, _ = env
    harness = SimulationHarness(service)
    original = service.remote.execute

    def lost(*args, **kwargs):
        service.remote.inject(args[0], args[1], "response_lost")
        return original(*args, **kwargs)

    service.remote.execute = lost
    assert harness.tick("t1", task.id)["status"] == "UNKNOWN"
    recording = export_audit(service, p, task.id)
    assert check_recording(recording)["unresolved"]
    replay = ReplayExecutor(recording)
    op = recording["body"]["operations"][0]
    with pytest.raises(DomainError, match="confirmed result"):
        replay.execute("t1", op["id"], **op["request"])
    assert replay.position == 0
    service.reconcile("t1", op["id"])
    assert not check_recording(export_audit(service, p, task.id))["unresolved"]


def test_rejected_and_cancelled_histories_export(env, task):
    recording = complete(env, task, reject=True)
    assert recording["body"]["task"]["result"] == "FAILED"
    assert any(
        o["attempts"] == 0 and o["status"] == "FAILED" for o in recording["body"]["operations"]
    )


def test_api_download_and_sse_resume_are_scoped(env, task):
    service, p, _ = env
    service.stop(p, task.id)
    service.finish("t1", task.id)
    token = issue_dev_token(service.settings, p)
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(service.settings, service.db, service.remote)) as client:
        url = f"/api/v1/tasks/{task.id}"
        recording = client.get(url + "/recording", headers=headers)
        assert recording.status_code == 200
        assert recording.headers["cache-control"] == "no-store"
        assert check_recording(recording.json())["event_count"] == 3
        events = client.get(url + "/events", headers={**headers, "Last-Event-ID": "1"})
        assert "id: 1\n" not in events.text and "id: 2\n" in events.text
        assert "event: stream.closed" in events.text
        assert client.get(url + "/events?after=-1", headers=headers).status_code == 422
        assert client.get(url + "/events?after=999", headers=headers).status_code == 409
        other = {
            "Authorization": "Bearer "
            + issue_dev_token(service.settings, p.model_copy(update={"tenant_id": "t2"}))
        }
        assert client.get(url + "/recording", headers=other).status_code == 404
        with service.db.session("t1") as s:
            s.scalar(select(Grant).where(Grant.subject == p.subject)).revoked = True
        assert client.get(url + "/recording", headers=headers).status_code == 403


def test_event_pages_recheck_expiry_grants_and_missing_rows(env, task):
    service, p, _ = env
    token = issue_dev_token(service.settings, p)
    assert event_page(service, token, task.id, 0)[0][0]["sequence"] == 1
    with pytest.raises(DomainError, match="expired"):
        event_page(service, issue_dev_token(service.settings, p, seconds=-1), task.id, 0)
    with service.db.session("t1") as s:
        s.delete(s.scalar(select(TaskEvent).where(TaskEvent.task_id == task.id)))
    with pytest.raises(DomainError, match="incomplete"):
        event_page(service, token, task.id, 0)


def test_stream_closes_and_clears_access_after_midstream_revocation(env, task, monkeypatch):
    import agent_py.event_feed as feed

    service, p, _ = env
    original = feed.event_page
    calls = []

    def revoke(*args):
        calls.append(1)
        if len(calls) > 1:
            raise DomainError("PERMISSION_REVOKED", "Revoked", 403)
        return original(*args)

    monkeypatch.setattr(feed, "event_page", revoke)
    with TestClient(create_app(service.settings, service.db, service.remote)) as client:
        response = client.get(
            f"/api/v1/tasks/{task.id}/events",
            headers={"Authorization": "Bearer " + issue_dev_token(service.settings, p)},
        )
    assert "event: access_revoked" in response.text and len(calls) == 2


def test_export_does_not_embed_artifacts_or_storage_locations(env, task):
    recording = complete(env, task)
    assert recording["body"]["artifacts"]
    assert all(set(a) == {"id", "kind", "digest"} for a in recording["body"]["artifacts"])
    assert "storage_key" not in json.dumps(recording)


def test_replay_does_not_equate_boolean_and_integer_parameters(env, task):
    recording = complete(env, task)
    op = recording["body"]["operations"][0]
    op["request"]["parameters"] = {"number": 1}
    op["request_digest"] = digest(op["request"])
    # Deliberately resealed input illustrates integrity is not authenticity.
    replay = ReplayExecutor(reseal(recording))
    with pytest.raises(DomainError, match="matching"):
        replay.execute(
            "t1", op["id"], op["request"]["tool"], op["request"]["resource"], {"number": True}
        )


def test_audit_cli_is_offline_and_reports_invalid_input(env, task, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import agent_py.cli as cli

    path = tmp_path / "audit.json"
    path.write_text(json.dumps(complete(env, task)))
    monkeypatch.setattr(
        cli, "build_service", lambda *args: pytest.fail("Offline CLI built a service")
    )
    runner = CliRunner()
    result = runner.invoke(cli.app, ["audit-check", str(path)])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["remote_state_verified"] is False
    path.write_text('{"body": {}, "digest": "invalid"}')
    invalid = runner.invoke(cli.app, ["audit-check", str(path)])
    assert invalid.exit_code == 1 and "RECORDING_INVALID" in invalid.output


def test_malformed_untrusted_payloads_produce_domain_errors(env, task):
    original = complete(env, task)
    for field, value in [("tool", []), ("parameters", False)]:
        recording = copy.deepcopy(original)
        op = recording["body"]["operations"][0]
        op["request"][field] = value
        op["request_digest"] = digest(op["request"])
        with pytest.raises(DomainError):
            check_recording(reseal(recording))
    recording = copy.deepcopy(original)
    event = next(e for e in recording["body"]["events"] if e["type"] == "approval.decided")
    event["payload"]["decision"] = []
    with pytest.raises(DomainError):
        check_recording(reseal(recording))
