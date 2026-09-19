"""Purpose-bound audit signatures with an independently provisioned offline trust store."""

import base64
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import Field, model_validator

from agent_py.audit import MAX_RECORDING_BYTES, check_recording
from agent_py.domain import Contract, DomainError, digest
from agent_py.jsonio import decode_json as decode_json
from agent_py.jsonio import load_json as load_json

DOMAIN = b"agent-py/audit-attestation/v1\x00"
MAX_SIGNED_BYTES = MAX_RECORDING_BYTES + 16_384


def timestamp(value: str):
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return when.astimezone(UTC)


class KeyPolicy(Contract):
    key_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    tenants: list[str] = Field(min_length=1, max_length=100)
    audiences: list[str] = Field(min_length=1, max_length=20)
    not_before: str = Field(max_length=40)
    not_after: str = Field(max_length=40)

    @model_validator(mode="after")
    def scope(self):
        if not all(0 < len(v) <= 160 and v != "*" for v in [*self.tenants, *self.audiences]):
            raise ValueError("Explicit nonempty tenant and audience scopes required")
        if timestamp(self.not_before) >= timestamp(self.not_after):
            raise ValueError("Invalid key validity window")
        return self


class SigningConfig(KeyPolicy):
    private_key_file: str = Field(min_length=1, max_length=4096)
    audience: str = Field(min_length=1, max_length=160)


class TrustedKey(KeyPolicy):
    public_key: str = Field(min_length=43, max_length=43)
    revoked: bool = False


class TrustStore(Contract):
    keys: list[TrustedKey] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique(self):
        if len({k.key_id for k in self.keys}) != len(self.keys):
            raise ValueError("Duplicate trusted key ID")
        for key in self.keys:
            decode64(key.public_key, 32)
        return self


class Attestation(Contract):
    algorithm: Literal["Ed25519"]
    key_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    tenant: str = Field(min_length=1, max_length=160)
    task_id: str = Field(min_length=1, max_length=36)
    audience: str = Field(min_length=1, max_length=160)
    issued_at: str = Field(max_length=40)
    recording_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class SignedRecording(Contract):
    schema_version: Literal["signed-recording-v1"] = Field(alias="schema")
    attestation: Attestation
    recording: dict
    signature: str = Field(min_length=86, max_length=86)


def encode64(value: bytes):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode64(value: str, length: int):
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if len(raw) != length or encode64(raw) != value:
        raise ValueError("Invalid canonical base64url value")
    return raw


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def private_key(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        mode = os.fstat(stream.fileno())
        if not stat.S_ISREG(mode.st_mode) or mode.st_mode & 0o077:
            raise ValueError("Private key must be a regular owner-only file")
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("Private key file too large")
    key = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Ed25519 private key required")
    return key


def sign_recording(recording, manifest: Path, *, when=None):
    check_recording(recording)
    when = when or datetime.now(UTC)
    try:
        config = SigningConfig.model_validate(load_json(manifest))
        tenant = recording["body"]["task"]["tenant"]
        if (
            tenant not in config.tenants
            or config.audience not in config.audiences
            or not timestamp(config.not_before) <= when < timestamp(config.not_after)
        ):
            raise ValueError("Signing scope or key validity denied")
        path = Path(config.private_key_file)
        key = private_key(path if path.is_absolute() else manifest.parent / path)
        claims = Attestation(
            algorithm="Ed25519",
            key_id=config.key_id,
            tenant=tenant,
            task_id=recording["body"]["task"]["id"],
            audience=config.audience,
            issued_at=when.astimezone(UTC).isoformat(),
            recording_digest=digest(recording),
        )
        attestation = claims.model_dump()
        return {
            "schema": "signed-recording-v1",
            "attestation": attestation,
            "recording": recording,
            "signature": encode64(key.sign(DOMAIN + canonical(attestation))),
        }
    except (OSError, ValueError, TypeError, RecursionError, UnsupportedAlgorithm):
        raise DomainError(
            "AUDIT_SIGNING_UNAVAILABLE",
            "Audit signing configuration, scope or key is unavailable",
            503,
        ) from None


def verify_recording(envelope, trust_path: Path, tenant: str, audience: str, *, when=None):
    when = when or datetime.now(UTC)
    try:
        if len(canonical(envelope)) > MAX_SIGNED_BYTES:
            raise ValueError("Signed recording too large")
        signed = SignedRecording.model_validate(envelope)
        trust = TrustStore.model_validate(load_json(trust_path))
        claims = signed.attestation
        trusted = next((k for k in trust.keys if k.key_id == claims.key_id), None)
        if not trusted or trusted.revoked:
            raise ValueError("Unknown or revoked signing key")
        issued = timestamp(claims.issued_at)
        if (
            claims.tenant != tenant
            or claims.audience != audience
            or tenant not in trusted.tenants
            or audience not in trusted.audiences
            or not timestamp(trusted.not_before) <= issued < timestamp(trusted.not_after)
            or issued > when + timedelta(seconds=60)
        ):
            raise ValueError("Signature scope or time policy denied")
        Ed25519PublicKey.from_public_bytes(decode64(trusted.public_key, 32)).verify(
            decode64(signed.signature, 64), DOMAIN + canonical(claims.model_dump())
        )
        if digest(signed.recording) != claims.recording_digest:
            raise ValueError("Signed recording digest mismatch")
        report = check_recording(signed.recording)
        if report["tenant"] != tenant or report["task_id"] != claims.task_id:
            raise ValueError("Signed task scope mismatch")
        return {
            **report,
            "signature_verified": True,
            "key_id": claims.key_id,
            "audience": audience,
            "issued_at": claims.issued_at,
            "trust_store_digest": digest(trust.model_dump()),
            "trusted_timestamp": False,
        }
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        InvalidSignature,
        RecursionError,
        UnsupportedAlgorithm,
    ) as exc:
        raise DomainError(
            "AUDIT_SIGNATURE_INVALID",
            "Signature or independent trust policy validation failed",
            422,
        ) from exc


def generate_key_files(directory: Path, key_id: str, tenant: str, audience: str, days: int):
    if not 1 <= days <= 365:
        raise ValueError("Key lifetime must be between 1 and 365 days")
    when = datetime.now(UTC)
    policy = KeyPolicy(
        key_id=key_id,
        tenants=[tenant],
        audiences=[audience],
        not_before=when.isoformat(),
        not_after=(when + timedelta(days=days)).isoformat(),
    ).model_dump()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    names = [directory / name for name in ("private.pem", "signer.json", "trust.json")]
    if any(p.exists() or p.is_symlink() for p in names):
        raise ValueError("Key output files already exist; use a new directory for rotation")
    key = Ed25519PrivateKey.generate()
    contents = [
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        canonical({**policy, "private_key_file": "private.pem", "audience": audience}),
        canonical(
            {
                "keys": [
                    {
                        **policy,
                        "public_key": encode64(key.public_key().public_bytes_raw()),
                        "revoked": False,
                    }
                ]
            }
        ),
    ]
    for path, content in zip(names, contents):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    return {"signer": str(names[1]), "trust_store": str(names[2])}
