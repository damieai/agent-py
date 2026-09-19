import copy
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_py.api import create_app
from agent_py.audit import export_audit
from agent_py.audit_signing import (
    DOMAIN,
    canonical,
    decode_json,
    encode64,
    generate_key_files,
    private_key,
    sign_recording,
    verify_recording,
)
from agent_py.cli import app
from agent_py.domain import DomainError, digest
from agent_py.replay import ReplayExecutor
from agent_py.security import issue_dev_token


@pytest.fixture
def signed_fixture(env, task, tmp_path):
    paths = generate_key_files(tmp_path / "keys", "test-2026", "t1", "agent-audit", 90)
    from pathlib import Path

    manifest, trust = Path(paths["signer"]), Path(paths["trust_store"])
    recording = export_audit(env[0], env[1], task.id)
    signed = sign_recording(recording, manifest)
    return signed, manifest, trust


def verify(signed_fixture):
    package, _, trust = signed_fixture
    return verify_recording(package, trust, "t1", "agent-audit")


def test_signed_roundtrip_and_replay_require_independent_verification(signed_fixture):
    package, _, trust = signed_fixture
    report = verify(signed_fixture)
    assert report["signature_verified"] and not report["remote_state_verified"]
    assert not report["trusted_timestamp"]
    with pytest.raises(DomainError, match="independent trust"):
        ReplayExecutor(package)
    ReplayExecutor.from_signed(package, trust, "t1", "agent-audit").assert_consumed()
    # Whitespace/key order in the outer transport JSON does not change canonical signing input.
    assert verify_recording(
        json.loads(json.dumps(package, sort_keys=True, indent=4)), trust, "t1", "agent-audit"
    )["signature_verified"]


@pytest.mark.parametrize(
    "mutation",
    ["body", "resealed", "tenant", "audience", "task", "algorithm", "key", "signature", "extra"],
)
def test_signature_rejects_package_and_claim_tampering(signed_fixture, mutation):
    original, _, trust = signed_fixture
    package = copy.deepcopy(original)
    if mutation in {"body", "resealed"}:
        package["recording"]["body"]["task"]["contract"]["goal"] = "Forged goal"
        if mutation == "resealed":
            package["recording"]["digest"] = digest(package["recording"]["body"])
            package["attestation"]["recording_digest"] = digest(package["recording"])
    elif mutation == "signature":
        package["signature"] = encode64(b"\0" * 64)
    elif mutation == "extra":
        package["public_key"] = "attacker supplied"
    else:
        field = {"task": "task_id", "key": "key_id"}.get(mutation, mutation)
        package["attestation"][field] = "other"
    with pytest.raises(DomainError):
        verify_recording(package, trust, "t1", "agent-audit")


@pytest.mark.parametrize("tenant,audience", [("t2", "agent-audit"), ("t1", "deployment")])
def test_valid_signature_cannot_be_reused_outside_expected_scope(signed_fixture, tenant, audience):
    package, _, trust = signed_fixture
    with pytest.raises(DomainError):
        verify_recording(package, trust, tenant, audience)


@pytest.mark.parametrize(
    "change", ["revoked", "public_key", "tenants", "audiences", "not_before", "duplicate"]
)
def test_current_trust_policy_overrides_valid_signature(signed_fixture, change):
    _, _, trust = signed_fixture
    data = json.loads(trust.read_text())
    key = data["keys"][0]
    if change == "revoked":
        key["revoked"] = True
    elif change == "public_key":
        key["public_key"] = encode64(b"x" * 32)
    elif change == "tenants":
        key["tenants"] = ["t2"]
    elif change == "audiences":
        key["audiences"] = ["other"]
    elif change == "not_before":
        key["not_before"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    else:
        data["keys"].append(key)
    trust.write_text(json.dumps(data))
    with pytest.raises(DomainError):
        verify(signed_fixture)


def test_rotation_accepts_old_key_until_explicit_revocation(signed_fixture, tmp_path):
    package, _, trust = signed_fixture
    next_paths = generate_key_files(tmp_path / "rotated", "test-next", "t1", "agent-audit", 90)
    from pathlib import Path

    next_manifest = Path(next_paths["signer"])
    data = json.loads(trust.read_text())
    data["keys"].extend(json.loads(Path(next_paths["trust_store"]).read_text())["keys"])
    trust.write_text(json.dumps(data))
    newer = sign_recording(package["recording"], next_manifest)
    assert verify(signed_fixture)["key_id"] == "test-2026"
    assert verify_recording(newer, trust, "t1", "agent-audit")["key_id"] == "test-next"
    data["keys"][0]["revoked"] = True
    trust.write_text(json.dumps(data))
    with pytest.raises(DomainError):
        verify(signed_fixture)
    assert verify_recording(newer, trust, "t1", "agent-audit")["signature_verified"]


def test_historical_signature_validity_and_future_time_policy(signed_fixture):
    package, manifest, trust = signed_fixture
    future = datetime.now(UTC) + timedelta(days=100)
    # Expired signing window does not invalidate a previously issued historical package.
    assert verify_recording(package, trust, "t1", "agent-audit", when=future)["signature_verified"]
    with pytest.raises(DomainError):
        sign_recording(package["recording"], manifest, when=future)
    future_package = sign_recording(
        package["recording"], manifest, when=datetime.now(UTC) + timedelta(minutes=5)
    )
    with pytest.raises(DomainError):
        verify_recording(future_package, trust, "t1", "agent-audit")


def test_private_permissions_symlinks_and_no_overwrite(signed_fixture):
    package, manifest, _ = signed_fixture
    key = manifest.parent / "private.pem"
    assert key.stat().st_mode & 0o777 == 0o600
    original = key.read_bytes()
    with pytest.raises(ValueError, match="already exist"):
        generate_key_files(manifest.parent, "new", "t1", "agent-audit", 90)
    assert key.read_bytes() == original
    os.chmod(key, 0o644)
    with pytest.raises(DomainError, match="configuration"):
        sign_recording(package["recording"], manifest)
    os.chmod(key, 0o600)
    linked = manifest.parent / "linked.pem"
    linked.symlink_to(key)
    data = json.loads(manifest.read_text())
    data["private_key_file"] = "linked.pem"
    manifest.write_text(json.dumps(data))
    with pytest.raises(DomainError):
        sign_recording(package["recording"], manifest)


def test_well_signed_invalid_task_binding_is_rejected(signed_fixture):
    original, manifest, trust = signed_fixture
    package = copy.deepcopy(original)
    package["attestation"]["task_id"] = "unrelated"
    package["signature"] = encode64(
        private_key(manifest.parent / "private.pem").sign(
            DOMAIN + canonical(package["attestation"])
        )
    )
    with pytest.raises(DomainError):
        verify_recording(package, trust, "t1", "agent-audit")


def test_offline_cli_refuses_unsigned_downgrade_and_requires_scope(
    signed_fixture, tmp_path, monkeypatch
):
    import agent_py.cli as cli

    package, _, trust = signed_fixture
    file = tmp_path / "package.json"
    file.write_text(json.dumps(package))
    monkeypatch.setattr(cli, "build_service", lambda *args: pytest.fail("Online service built"))
    runner = CliRunner()
    arguments = [
        "audit-check",
        str(file),
        "--trust-store",
        str(trust),
        "--tenant",
        "t1",
        "--audience",
        "agent-audit",
    ]
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["signature_verified"]
    assert runner.invoke(app, ["audit-check", str(file)]).exit_code != 0
    file.write_text(json.dumps(package["recording"]))
    assert runner.invoke(app, arguments).exit_code != 0


def test_signed_api_has_no_unsigned_fallback_and_rechecks_tenant(env, task, signed_fixture):
    service, p, _ = env
    _, manifest, trust = signed_fixture
    headers = {"Authorization": "Bearer " + issue_dev_token(service.settings, p)}
    path = f"/api/v1/tasks/{task.id}/recording?signed=true"
    with TestClient(create_app(service.settings, service.db, service.remote)) as client:
        assert client.get(path, headers=headers).status_code == 503
        service.settings.audit_signing_manifest = manifest
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert verify_recording(response.json(), trust, "t1", "agent-audit")["signature_verified"]
        policy = json.loads(manifest.read_text())
        policy["tenants"] = ["t2"]
        manifest.write_text(json.dumps(policy))
        assert client.get(path, headers=headers).status_code == 503
        other = {
            "Authorization": "Bearer "
            + issue_dev_token(service.settings, p.model_copy(update={"tenant_id": "t2"}))
        }
        assert client.get(path, headers=other).status_code == 404


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_ambiguous_json_is_rejected_before_verification(raw):
    with pytest.raises(ValueError):
        decode_json(raw)
