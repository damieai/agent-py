import copy
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_releases import contract
from test_releases import pinned as pinned
from test_releases import release_evidence as release_evidence
from typer.testing import CliRunner

from agent_py.api import create_app
from agent_py.audit_signing import DOMAIN as AUDIT_DOMAIN
from agent_py.audit_signing import canonical, encode64, private_key, timestamp
from agent_py.cli import app
from agent_py.config import Settings
from agent_py.domain import ActionProposal, DomainError
from agent_py.release_signing import (
    DOMAIN,
    generate_release_keys,
    sign_release,
    verify_release_attestation,
    write_private_json,
)
from agent_py.releases import ReleaseGuard, release_id
from agent_py.service import Service


@pytest.fixture
def signed(pinned, tmp_path):
    service, owner, _, manifest = pinned
    paths = generate_release_keys(tmp_path / "keys", "release-test", "agent-staging", "test")
    signer, trust = Path(paths["signer"]), Path(paths["trust_store"])
    attestation = tmp_path / "attestation.json"
    envelope = sign_release(manifest, signer, "agent-staging", "test")
    write_private_json(attestation, envelope)
    settings = service.settings.model_copy(
        update={
            "release_attestation": attestation,
            "release_trust_store": trust,
            "release_audience": "agent-staging",
        }
    )
    active = Service(service.db, settings, service.remote)
    yield active, owner, manifest, signer, trust, attestation, envelope
    active.telemetry.close()


def verify(signed, **kwargs):
    service, _, _, _, trust, path, _ = signed
    return verify_release_attestation(
        path, trust, service.release.id, "agent-staging", "test", **kwargs
    )


def test_signed_startup_and_current_policy_check(signed):
    service, owner, manifest, _, _, _, _ = signed
    result = verify(signed)
    assert result["signature_verified"]
    assert not result["production_ready"] and not result["trusted_timestamp"]
    assert result["release_id"] == release_id(manifest) == service.release.id
    assert service.release.signature_required
    task = service.create_task(owner, contract(), "signed")
    assert service.reserve("t1", task.id, "signed-budget", 1)


@pytest.mark.parametrize(
    "mutation",
    [
        "release_id",
        "audience",
        "environment",
        "key_id",
        "algorithm",
        "signature",
        "extra",
        "issued_at",
        "expires_at",
        "domain",
        "duplicate",
    ],
)
def test_tampered_attestations_and_domain_confusion_rejected(signed, mutation):
    _, _, _, signer, _, path, envelope = signed
    value = copy.deepcopy(envelope)
    if mutation == "signature":
        value["signature"] = encode64(b"\0" * 64)
    elif mutation == "extra":
        value["public_key"] = "self-authorized"
    elif mutation == "domain":
        value["signature"] = encode64(
            private_key(signer.parent / "private.pem").sign(
                AUDIT_DOMAIN + canonical(value["claims"])
            )
        )
    elif mutation == "duplicate":
        path.write_text('{"schema":"release-attestation-v1",' + json.dumps(value)[1:])
        with pytest.raises(DomainError):
            verify(signed)
        return
    else:
        value["claims"][mutation] = {
            "release_id": "sha256:" + "f" * 64,
            "audience": "other",
            "environment": "production",
            "key_id": "other",
            "algorithm": "HS256",
            "issued_at": "2020-01-01T00:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }[mutation]
    path.write_text(json.dumps(value))
    with pytest.raises(DomainError):
        verify(signed)


@pytest.mark.parametrize(
    "mutation",
    [
        "revoked",
        "missing_key",
        "public_key",
        "audiences",
        "environments",
        "duplicate_key",
        "not_before",
        "not_after",
        "lifetime",
        "missing_file",
    ],
)
def test_current_trust_policy_denies_previously_valid_signature(signed, mutation):
    service, owner, _, _, trust, _, _ = signed
    task = service.create_task(owner, contract(), "revoke")
    data = json.loads(trust.read_text())
    key = data["keys"][0]
    if mutation == "missing_file":
        trust.unlink()
    else:
        if mutation == "revoked":
            key["revoked"] = True
        elif mutation == "missing_key":
            key["key_id"] = "unrelated"
        elif mutation == "public_key":
            key["public_key"] = encode64(b"x" * 32)
        elif mutation == "audiences":
            key["audiences"] = ["other"]
        elif mutation == "environments":
            key["environments"] = ["production"]
        elif mutation == "duplicate_key":
            data["keys"].append(key)
        elif mutation == "not_before":
            key["not_before"] = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        elif mutation == "not_after":
            key["not_after"] = datetime.now(UTC).isoformat()
        elif mutation == "lifetime":
            data["maximum_lifetime_seconds"] = 60
        trust.write_text(json.dumps(data))
    with pytest.raises(DomainError) as error:
        service.reserve("t1", task.id, "revoked", 1)
    assert error.value.code == "RELEASE_SIGNATURE_INVALID"
    with pytest.raises(DomainError):
        ReleaseGuard(service.settings)
    # No signing trust is needed to stop an already-created task.
    service.stop(owner, task.id)
    assert service.finish("t1", task.id).result == "CANCELLED"


@pytest.mark.parametrize("point", ["before_issue", "at_expiry", "after_expiry"])
def test_time_boundaries(signed, point):
    envelope = signed[-1]
    issued = timestamp(envelope["claims"]["issued_at"])
    expires = timestamp(envelope["claims"]["expires_at"])
    when = {
        "before_issue": issued - timedelta(microseconds=1),
        "at_expiry": expires,
        "after_expiry": expires + timedelta(seconds=1),
    }[point]
    with pytest.raises(DomainError):
        verify(signed, when=when)
    assert verify(signed, when=issued)["signature_verified"]
    assert verify(signed, when=expires - timedelta(microseconds=1))["signature_verified"]


def test_signed_invalid_time_window_rejected_even_with_valid_crypto(signed):
    _, _, _, signer, _, path, envelope = signed
    altered = copy.deepcopy(envelope)
    altered["claims"]["expires_at"] = altered["claims"]["issued_at"]
    altered["signature"] = encode64(
        private_key(signer.parent / "private.pem").sign(DOMAIN + canonical(altered["claims"]))
    )
    path.write_text(json.dumps(altered))
    with pytest.raises(DomainError):
        verify(signed)


def test_key_rotation_and_renewal_keep_release_and_queue_identity(signed, tmp_path):
    service, owner, manifest, _, trust, path, _ = signed
    identity, queue = service.release.id, service.release.task_queue
    task = service.create_task(owner, contract(), "rotation")
    new = generate_release_keys(tmp_path / "new", "release-next", "agent-staging", "test")
    previous = json.loads(trust.read_text())
    new_key = json.loads(Path(new["trust_store"]).read_text())["keys"][0]
    previous["keys"].append(new_key)
    trust.write_text(json.dumps(previous))
    assert verify(signed)["key_id"] == "release-test"
    replacement = sign_release(manifest, Path(new["signer"]), "agent-staging", "test")
    next_file = path.with_suffix(".next")
    write_private_json(next_file, replacement)
    os.replace(next_file, path)
    previous["keys"][0]["revoked"] = True
    trust.write_text(json.dumps(previous))
    assert verify(signed)["key_id"] == "release-next"
    assert service.reserve("t1", task.id, "renewed", 1)
    assert (service.release.id, service.release.task_queue) == (identity, queue)


@pytest.mark.parametrize(
    "field,value",
    [
        ("release_attestation", None),
        ("release_trust_store", None),
        ("release_audience", "other"),
        ("environment", "production"),
    ],
)
def test_running_service_cannot_silently_drop_signature_requirement(signed, field, value):
    service = signed[0]
    setattr(service.settings, field, value)
    with pytest.raises(DomainError):
        service.release.check()


def test_unsigned_manifest_cannot_replace_attestation(signed):
    service, _, _, _, _, path, _ = signed
    path.write_bytes(service.settings.release_manifest.read_bytes())
    with pytest.raises(DomainError):
        service.release.check()


def test_revocation_readiness_fails_but_unknown_effect_still_reconciles(signed):
    service, owner, _, _, trust, _, _ = signed
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
    app = create_app(service.settings, service.db, service.remote)
    with TestClient(app) as client:
        assert client.get("/health/live").json()["release_signature_required"]
        assert client.get("/health/ready").status_code == 200
        data = json.loads(trust.read_text())
        data["keys"][0]["revoked"] = True
        trust.write_text(json.dumps(data))
        assert client.get("/health/ready").status_code == 503
        assert service.reconcile("t1", op.id).status == "SUCCEEDED"


@pytest.mark.parametrize("kind", ["fifo", "symlink", "large"])
def test_signature_and_trust_files_are_bounded_regular_files(signed, tmp_path, kind):
    _, _, _, _, _, path, _ = signed
    raw = path.read_bytes()
    path.unlink()
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(raw)
        path.symlink_to(target)
    else:
        path.write_bytes(b" " * 16_385)
    with pytest.raises(DomainError):
        verify(signed)


@pytest.mark.parametrize("mutation", ["audience", "environment", "lifetime", "permissions"])
def test_signer_enforces_scope_lifetime_and_private_key_protection(signed, mutation):
    _, _, manifest, signer, _, _, _ = signed
    kwargs = {"audience": "agent-staging", "environment": "test", "lifetime": 3600}
    if mutation == "permissions":
        (signer.parent / "private.pem").chmod(0o644)
    else:
        kwargs[mutation] = {"audience": "other", "environment": "production", "lifetime": 604801}[
            mutation
        ]
    with pytest.raises(DomainError):
        sign_release(manifest, signer, **kwargs)


def test_signature_settings_must_be_complete_and_require_release(tmp_path):
    for values in (
        {"release_attestation": tmp_path / "signature.json"},
        {"release_trust_store": tmp_path / "trust.json"},
        {"release_audience": "staging"},
        {
            "release_attestation": tmp_path / "s.json",
            "release_trust_store": tmp_path / "t.json",
            "release_audience": "staging",
        },
    ):
        with pytest.raises(ValueError, match="signature requires"):
            Settings(**values, _env_file=None)


def test_cli_keygen_sign_verify_and_check_are_offline(signed, tmp_path, monkeypatch):
    service, _, _, signer, trust, _, _ = signed
    monkeypatch.setattr("agent_py.cli.get_settings", lambda: service.settings)
    from agent_py.db import Database

    monkeypatch.setattr(Database, "__init__", lambda *a, **k: pytest.fail("Database touched"))
    runner = CliRunner()
    keygen = ["release-keygen", str(tmp_path / "cli-keys"), "cli-key", "agent-staging"]
    assert runner.invoke(app, keygen).exit_code == 0
    assert runner.invoke(app, keygen).exit_code == 1
    target = tmp_path / "cli-attestation.json"
    args = [
        "release-sign",
        str(service.settings.release_manifest),
        str(signer),
        service.release.id,
        str(target),
        "agent-staging",
    ]
    assert runner.invoke(app, args).exit_code == 0
    assert runner.invoke(app, args).exit_code == 1
    verified = runner.invoke(
        app, ["release-verify", str(target), str(trust), service.release.id, "agent-staging"]
    )
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.output)["signature_verified"]
    denied = runner.invoke(
        app, ["release-verify", str(target), str(trust), service.release.id, "other"]
    )
    assert denied.exit_code == 1
    checked = runner.invoke(
        app, ["release-check", str(service.settings.release_manifest), service.release.id]
    )
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.output)["signature_required"]


def test_revoked_signer_blocks_outbox_without_losing_delivery_obligation(signed):
    import asyncio
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from agent_py.db import Outbox
    from agent_py.runtime import dispatch_once

    service, owner, _, _, trust, _, _ = signed
    task = service.create_task(owner, contract(), "dispatch-revoked")
    data = json.loads(trust.read_text())
    data["keys"][0]["revoked"] = True
    trust.write_text(json.dumps(data))
    client = AsyncMock()
    with pytest.raises(DomainError) as error:
        asyncio.run(dispatch_once(service, client, "t1"))
    assert error.value.code == "RELEASE_SIGNATURE_INVALID"
    client.start_workflow.assert_not_awaited()
    with service.db.session("t1") as session:
        assert not session.scalar(select(Outbox).where(Outbox.task_id == task.id)).delivered


def test_independent_release_pin_remains_mandatory(signed):
    service = signed[0]
    settings = service.settings.model_copy(update={"release_expected_id": "sha256:" + "a" * 64})
    with pytest.raises(DomainError, match="independent deployment pin"):
        ReleaseGuard(settings)


def test_attestation_expiry_is_rechecked_by_running_guard(signed):
    service, _, manifest, signer, trust, path, _ = signed
    earlier = datetime.now(UTC) - timedelta(hours=2)
    signer_data = json.loads(signer.read_text())
    signer_data["not_before"] = (earlier - timedelta(hours=1)).isoformat()
    signer.write_text(json.dumps(signer_data))
    trust_data = json.loads(trust.read_text())
    trust_data["keys"][0]["not_before"] = signer_data["not_before"]
    trust.write_text(json.dumps(trust_data))
    expired = sign_release(manifest, signer, "agent-staging", "test", when=earlier)
    path.write_text(json.dumps(expired))
    with pytest.raises(DomainError) as error:
        service.release.check()
    assert error.value.code == "RELEASE_SIGNATURE_INVALID"
