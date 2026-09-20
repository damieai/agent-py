"""Short-lived release authorization attestations, separate from audit signatures."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import Field, model_validator

from agent_py.audit_signing import canonical, decode64, encode64, private_key, timestamp
from agent_py.domain import Contract, DomainError, digest
from agent_py.jsonio import decode_json

DOMAIN = b"agent-py/release-attestation/v1\x00"
Environment = Literal["development", "test", "production"]


class ReleaseKeyPolicy(Contract):
    key_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    audiences: list[str] = Field(min_length=1, max_length=20)
    environments: list[Environment] = Field(min_length=1, max_length=3)
    not_before: str = Field(max_length=40)
    not_after: str = Field(max_length=40)

    @model_validator(mode="after")
    def scope(self):
        if (
            len(set(self.audiences)) != len(self.audiences)
            or len(set(self.environments)) != len(self.environments)
            or not all(0 < len(a) <= 160 and a != "*" for a in self.audiences)
            or timestamp(self.not_before) >= timestamp(self.not_after)
        ):
            raise ValueError("Invalid release key scope or validity")
        return self


class ReleaseSigner(ReleaseKeyPolicy):
    private_key_file: str = Field(min_length=1, max_length=4096)


class ReleaseTrustedKey(ReleaseKeyPolicy):
    public_key: str = Field(min_length=43, max_length=43)
    revoked: bool = False


class ReleaseTrust(Contract):
    schema_version: Literal["release-trust-v1"] = Field(alias="schema")
    maximum_lifetime_seconds: int = Field(default=86400, ge=60, le=604800)
    keys: list[ReleaseTrustedKey] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique(self):
        if len({k.key_id for k in self.keys}) != len(self.keys):
            raise ValueError("Duplicate release key ID")
        for key in self.keys:
            decode64(key.public_key, 32)
        return self


class ReleaseClaims(Contract):
    algorithm: Literal["Ed25519"]
    key_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    release_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    audience: str = Field(min_length=1, max_length=160)
    environment: Environment
    issued_at: str = Field(max_length=40)
    expires_at: str = Field(max_length=40)


class ReleaseAttestation(Contract):
    schema_version: Literal["release-attestation-v1"] = Field(alias="schema")
    claims: ReleaseClaims
    signature: str = Field(min_length=86, max_length=86)


def local_json(path, limit):
    # Lazy import avoids the ReleaseGuard/signing dependency cycle.
    from agent_py.releases import read_regular

    return decode_json(read_regular(path, limit))


def sign_release(release, signer_path: Path, audience, environment, lifetime=3600, *, when=None):
    from agent_py.releases import check_evidence, release_id

    when = when or datetime.now(UTC)
    try:
        check_evidence(release)
        config = ReleaseSigner.model_validate(local_json(signer_path, 100_000))
        if type(lifetime) is not int or not 60 <= lifetime <= 604800:
            raise ValueError("Invalid attestation lifetime")
        expires = when + timedelta(seconds=lifetime)
        if (
            audience not in config.audiences
            or environment not in config.environments
            or not timestamp(config.not_before) <= when < expires <= timestamp(config.not_after)
        ):
            raise ValueError("Signing scope or time denied")
        path = Path(config.private_key_file)
        key = private_key(path if path.is_absolute() else signer_path.parent / path)
        claims = ReleaseClaims(
            algorithm="Ed25519",
            key_id=config.key_id,
            release_id=release_id(release),
            audience=audience,
            environment=environment,
            issued_at=when.astimezone(UTC).isoformat(),
            expires_at=expires.astimezone(UTC).isoformat(),
        )
        return ReleaseAttestation.model_validate(
            {
                "schema": "release-attestation-v1",
                "claims": claims.model_dump(),
                "signature": encode64(key.sign(DOMAIN + canonical(claims.model_dump()))),
            }
        ).model_dump(by_alias=True)
    except (OSError, ValueError, TypeError, RecursionError, UnsupportedAlgorithm):
        raise DomainError(
            "RELEASE_SIGNING_UNAVAILABLE", "Release signing policy or key denied", 503
        ) from None


def verify_release_attestation(path, trust_path, expected_id, audience, environment, *, when=None):
    when = when or datetime.now(UTC)
    try:
        signed = ReleaseAttestation.model_validate(local_json(path, 16_384))
        trust = ReleaseTrust.model_validate(local_json(trust_path, 100_000))
        claims = signed.claims
        key = next((k for k in trust.keys if k.key_id == claims.key_id), None)
        issued, expires = timestamp(claims.issued_at), timestamp(claims.expires_at)
        if (
            key is None
            or key.revoked
            or claims.release_id != expected_id
            or claims.audience != audience
            or claims.environment != environment
            or audience not in key.audiences
            or environment not in key.environments
            or not timestamp(key.not_before) <= issued <= when < expires <= timestamp(key.not_after)
            or not 60 <= (expires - issued).total_seconds() <= trust.maximum_lifetime_seconds
        ):
            raise ValueError("Release trust, scope or validity denied")
        Ed25519PublicKey.from_public_bytes(decode64(key.public_key, 32)).verify(
            decode64(signed.signature, 64), DOMAIN + canonical(claims.model_dump())
        )
        return {
            "release_id": expected_id,
            "key_id": key.key_id,
            "signature_verified": True,
            "audience": audience,
            "environment": environment,
            "expires_at": claims.expires_at,
            "trust_store_digest": digest(trust.model_dump(by_alias=True)),
            "production_ready": False,
            "trusted_timestamp": False,
        }
    except (OSError, ValueError, TypeError, RecursionError, InvalidSignature, UnsupportedAlgorithm):
        raise DomainError(
            "RELEASE_SIGNATURE_INVALID",
            "Release signature or current independent trust policy denied",
            409,
        ) from None


def write_private_json(path, value):
    data = canonical(value) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def generate_release_keys(directory, key_id, audience, environment, days=90):
    if type(days) is not int or not 1 <= days <= 365:
        raise ValueError("Key lifetime must be 1..365 days")
    when = datetime.now(UTC)
    policy = ReleaseKeyPolicy(
        key_id=key_id,
        audiences=[audience],
        environments=[environment],
        not_before=when.isoformat(),
        not_after=(when + timedelta(days=days)).isoformat(),
    ).model_dump()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = [directory / name for name in ("private.pem", "signer.json", "trust.json")]
    if any(p.exists() or p.is_symlink() for p in paths):
        raise ValueError("Use a new directory for each release key")
    key = Ed25519PrivateKey.generate()
    fd = os.open(paths[0], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        stream.flush()
        os.fsync(stream.fileno())
    write_private_json(paths[1], {**policy, "private_key_file": "private.pem"})
    write_private_json(
        paths[2],
        {
            "schema": "release-trust-v1",
            "maximum_lifetime_seconds": 86400,
            "keys": [
                {
                    **policy,
                    "public_key": encode64(key.public_key().public_bytes_raw()),
                    "revoked": False,
                }
            ],
        },
    )
    return {"signer": str(paths[1]), "trust_store": str(paths[2])}
