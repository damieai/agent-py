import base64
import json
import os
import time

import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from agent_py.api import create_app
from agent_py.config import Settings
from agent_py.db import Grant
from agent_py.domain import DomainError
from agent_py.event_feed import event_page
from agent_py.security import authenticate, issue_dev_token


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def jwk(key, kid):
    return {
        **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
        "key_ops": ["verify"],
    }


def replace_keys(path, keys):
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps({"keys": keys}))
    os.replace(temporary, path)


@pytest.fixture(scope="module")
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


@pytest.fixture
def auth(env, tmp_path, keys):
    service, principal, _ = env
    path = tmp_path / "jwks.json"
    replace_keys(path, [jwk(keys[0], "old")])
    service.settings.auth_jwks_file = path
    service.settings.auth_issuer = "https://identity.example.test/"
    service.settings.auth_audience = "agent-api"
    return service, principal, path


def token(auth, key, kid="old", *, header=None, claims=None):
    service, principal, _ = auth
    when = int(time.time())
    values = {
        **principal.model_dump(),
        "sub": principal.subject,
        "iss": service.settings.auth_issuer,
        "aud": service.settings.auth_audience,
        "iat": when,
        "exp": when + 300,
    }
    values.update(claims or {})
    return jwt.encode(values, key, algorithm="RS256", headers={"kid": kid, **(header or {})})


def denied(settings, credential, code="UNAUTHENTICATED"):
    with pytest.raises(DomainError) as error:
        authenticate(settings, credential)
    assert error.value.code == code


def test_overlap_rotation_and_removal_have_no_stale_cache(auth, keys):
    service, principal, path = auth
    old, new = token(auth, keys[0]), token(auth, keys[1], "new")
    assert authenticate(service.settings, old) == principal
    denied(service.settings, new)
    replace_keys(path, [jwk(keys[0], "old"), jwk(keys[1], "new")])
    assert authenticate(service.settings, old) == authenticate(service.settings, new) == principal
    replace_keys(path, [jwk(keys[1], "new")])
    denied(service.settings, old)
    assert authenticate(service.settings, new) == principal
    assert "AUTH_UNCONFIGURED" not in new


@pytest.mark.parametrize(
    "mutation",
    [
        "issuer",
        "audience",
        "expired",
        "future",
        "nbf",
        "long",
        "reverse",
        "string_time",
        "float_time",
        "bool_time",
        "string_nbf",
        "no_exp",
        "empty_subject",
        "empty_tenant",
        "roles_type",
        "oversize_scope",
    ],
)
def test_signed_invalid_claims_are_rejected(auth, keys, mutation):
    when = int(time.time())
    values = {
        "issuer": {"iss": "https://attacker.test/"},
        "audience": {"aud": "another-api"},
        "expired": {"iat": when - 600, "exp": when - 300},
        "future": {"iat": when + 300, "exp": when + 600},
        "nbf": {"nbf": when + 300},
        "long": {"exp": when + 3601},
        "reverse": {"exp": when - 1},
        "string_time": {"iat": str(when)},
        "float_time": {"iat": float(when)},
        "bool_time": {"iat": True},
        "string_nbf": {"nbf": str(when)},
        "no_exp": {"exp": None},
        "empty_subject": {"sub": ""},
        "empty_tenant": {"tenant_id": ""},
        "roles_type": {"roles": "operator"},
        "oversize_scope": {"projects": ["x" * 81]},
    }[mutation]
    denied(auth[0].settings, token(auth, keys[0], claims=values))


@pytest.mark.parametrize(
    "header",
    [
        {"jku": "https://attacker.test/keys"},
        {"jwk": {"kty": "RSA"}},
        {"x5u": "https://attacker.test/cert"},
        {"crit": ["x"], "x": True},
        {"b64": False},
        {"typ": "id_token"},
        {"kid": "../../etc/passwd"},
        {"kid": None},
    ],
)
def test_headers_cannot_choose_trust_or_token_purpose(auth, keys, header):
    if header.get("kid", "") is None:
        # PyJWT refuses to create this header; use a manually signed adversarial JWT below.
        credential = raw_token(
            keys[0], {"alg": "RS256", "typ": "JWT", "kid": None}, {"iat": 0, "exp": 1}
        )
    else:
        credential = token(auth, keys[0], header=header)
    denied(auth[0].settings, credential)


def raw_token(key, header, payload):
    h = header if isinstance(header, str) else json.dumps(header)
    p = payload if isinstance(payload, str) else json.dumps(payload)
    signing_input = (b64(h.encode()) + "." + b64(p.encode())).encode()
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode() + "." + b64(signature)


@pytest.mark.parametrize(
    "part", ["header", "payload", "nan", "utf16", "oversize", "signature_padding"]
)
def test_ambiguous_json_and_noncanonical_transport_are_rejected(auth, keys, part):
    header = {"alg": "RS256", "kid": "old", "typ": "JWT"}
    payload = jwt.decode(token(auth, keys[0]), options={"verify_signature": False})
    if part == "header":
        header = '{"alg":"HS256","alg":"RS256","kid":"old","typ":"JWT"}'
    if part == "payload":
        payload = json.dumps(payload)[:-1] + ',"tenant_id":"t2"}'
    if part == "nan":
        payload = json.dumps(payload)[:-1] + ',"extra":NaN}'
    credential = raw_token(keys[0], header, payload)
    if part == "utf16":
        segments = credential.split(".")
        segments[1] = b64(json.dumps(payload).encode("utf-16"))
        signing_input = ".".join(segments[:2]).encode()
        segments[2] = b64(keys[0].sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))
        credential = ".".join(segments)
    if part == "oversize":
        credential = "x" * 16385
    if part == "signature_padding":
        credential += "="
    denied(auth[0].settings, credential)


@pytest.mark.parametrize(
    "bad",
    [
        "empty",
        "duplicate",
        "private",
        "wrong_alg",
        "wrong_use",
        "sign_ops",
        "bad_integer",
        "weak",
        "unknown_field",
        "oversize",
        "malformed",
    ],
)
def test_invalid_keysets_fail_closed_without_secret_fallback(auth, keys, bad):
    service, _, path = auth
    entry = jwk(keys[0], "old")
    contents = {"keys": [entry]}
    if bad == "empty":
        contents["keys"] = []
    elif bad == "duplicate":
        contents["keys"] *= 2
    elif bad == "private":
        entry["d"] = "private-key-material"
    elif bad == "wrong_alg":
        entry["alg"] = "HS256"
    elif bad == "wrong_use":
        entry["use"] = "enc"
    elif bad == "sign_ops":
        entry["key_ops"] = ["sign"]
    elif bad == "bad_integer":
        entry["n"] = "AA" + entry["n"]
    elif bad == "weak":
        entry["n"] = b64(b"\xff" * 128)
    elif bad == "unknown_field":
        entry["x5u"] = "https://attacker.test/"
    path.write_text(json.dumps(contents))
    if bad == "oversize":
        path.write_text(" " * 100001)
    if bad == "malformed":
        path.write_text('{"keys":[],"keys":[]}')
    denied(service.settings, token(auth, keys[0]), "AUTH_UNCONFIGURED")


def test_unavailable_or_fifo_keysets_never_reuse_previous_keys(auth, keys):
    service, _, path = auth
    credential = token(auth, keys[0])
    authenticate(service.settings, credential)
    path.unlink()
    denied(service.settings, credential, "AUTH_UNCONFIGURED")
    os.mkfifo(path)
    denied(service.settings, credential, "AUTH_UNCONFIGURED")


def test_algorithm_confusion_and_missing_kid_rejected(auth, keys):
    denied(auth[0].settings, token(auth, keys[1], "old"))
    values = jwt.decode(token(auth, keys[0]), options={"verify_signature": False})
    der = (
        keys[0]
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    import hashlib
    import hmac

    signing_input = (
        b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "old"}).encode())
        + "."
        + b64(json.dumps(values).encode())
    ).encode()
    forged = (
        signing_input.decode() + "." + b64(hmac.new(der, signing_input, hashlib.sha256).digest())
    )
    denied(auth[0].settings, forged)
    denied(auth[0].settings, jwt.encode(values, keys[0], algorithm="RS256"))
    denied(auth[0].settings, jwt.encode(values, "", algorithm="none"))


def test_rotation_is_enforced_for_api_and_each_sse_page(auth, keys, task):
    service, _, path = auth
    old = token(auth, keys[0])
    client = TestClient(create_app(service.settings, service.db, service.remote))
    with client:
        assert (
            client.get(
                f"/api/v1/tasks/{task.id}", headers={"Authorization": "Bearer " + old}
            ).status_code
            == 200
        )
        assert event_page(service, old, task.id, 0)[0]
        replace_keys(path, [jwk(keys[1], "new")])
        assert (
            client.get(
                f"/api/v1/tasks/{task.id}", headers={"Authorization": "Bearer " + old}
            ).status_code
            == 401
        )
        with pytest.raises(DomainError):
            event_page(service, old, task.id, 0)
        new = token(auth, keys[1], "new")
        assert (
            client.get(
                f"/api/v1/tasks/{task.id}", headers={"Authorization": "Bearer " + new}
            ).status_code
            == 200
        )
        with service.db.session("t1") as s:
            for grant in s.query(Grant).filter_by(tenant_id="t1"):
                grant.revoked = True
        assert (
            client.get(
                f"/api/v1/tasks/{task.id}", headers={"Authorization": "Bearer " + new}
            ).status_code
            == 403
        )


def test_static_key_compatibility_and_development_lifetime(env, keys):
    service, principal, _ = env
    service.settings.auth_max_token_lifetime_seconds = 120
    service.settings.auth_token_type = "at+jwt"
    development = issue_dev_token(service.settings, principal)
    assert authenticate(service.settings, development) == principal
    claims = jwt.decode(development, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 120
    service.settings.auth_public_key = (
        keys[0]
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    credential = jwt.encode(claims, keys[0], algorithm="RS256", headers={"typ": "at+jwt"})
    assert authenticate(service.settings, credential) == principal
    with pytest.raises(ValueError, match="HS256"):
        issue_dev_token(service.settings, principal)


def test_production_config_requires_one_trust_source_and_external_issuer(tmp_path):
    settings = dict(
        environment="production",
        database_url="postgresql://host/db",
        auth_issuer="https://idp.test/",
        _env_file=None,
    )
    assert Settings(**settings, auth_jwks_file=tmp_path / "keys.json")
    with pytest.raises(ValueError):
        Settings(**settings)
    with pytest.raises(ValueError):
        Settings(**settings, auth_jwks_file=tmp_path / "keys.json", auth_public_key="key")
    with pytest.raises(ValueError):
        Settings(
            **{**settings, "auth_issuer": "agent-py-local"}, auth_jwks_file=tmp_path / "keys.json"
        )


def test_readiness_detects_keyset_failure_without_disclosing_paths(auth, keys):
    service, _, path = auth
    with TestClient(create_app(service.settings, service.db, service.remote)) as client:
        assert client.get("/health/ready").json()["scope"] == "api-database-and-local-jwks"
        path.write_text("private corrupt configuration")
        result = client.get("/health/ready")
        assert result.status_code == 503
        assert str(path) not in result.text and "private" not in result.text
        assert client.get("/health/live").status_code == 200
        replace_keys(path, [jwk(keys[0], "old")])
        assert client.get("/health/ready").status_code == 200


def test_keyset_check_is_offline_and_outputs_only_public_fingerprints(auth, monkeypatch):
    from typer.testing import CliRunner

    from agent_py.cli import app

    service, _, path = auth
    monkeypatch.setattr("agent_py.cli.build_service", lambda *_: pytest.fail("Must remain offline"))
    result = CliRunner().invoke(app, ["auth-keys-check", str(path)])
    assert result.exit_code == 0
    row = json.loads(result.stdout)["keys"][0]
    assert row["kid"] == "old" and row["bits"] == 2048 and len(row["spki_sha256"]) == 64
    assert set(row) == {"kid", "algorithm", "bits", "spki_sha256"}
    path.unlink()
    assert CliRunner().invoke(app, ["auth-keys-check", str(path)]).exit_code == 1


def test_bounded_clock_skew_and_projected_config_symlink(auth, keys, tmp_path):
    service, principal, path = auth
    alias = tmp_path / "projected.json"
    alias.symlink_to(path)
    service.settings.auth_jwks_file = alias
    service.settings.auth_clock_skew_seconds = 30
    when = int(time.time())
    credential = token(auth, keys[0], claims={"iat": when - 300, "exp": when - 10})
    assert authenticate(service.settings, credential) == principal
    denied(service.settings, token(auth, keys[0], claims={"iat": when - 300, "exp": when - 60}))
    replace_keys(path, [jwk(keys[1], "new")])
    denied(service.settings, credential)
