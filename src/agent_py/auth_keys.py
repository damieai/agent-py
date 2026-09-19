"""Bounded local JWKS trust; token headers cannot select files, URLs, or algorithms."""

import base64
import os
import stat
from pathlib import Path
from typing import Literal

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import Field

from agent_py.domain import Contract, DomainError
from agent_py.jsonio import decode_json


def unpad64(value: str):
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != value:
        raise ValueError("Noncanonical base64url")
    return raw


class PublicJWK(Contract):
    kty: Literal["RSA"]
    kid: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    n: str = Field(min_length=1, max_length=1366)
    e: str = Field(min_length=1, max_length=12)
    alg: Literal["RS256"] = "RS256"
    use: Literal["sig"] = "sig"
    key_ops: list[Literal["verify"]] = Field(
        default_factory=lambda: ["verify"], min_length=1, max_length=1
    )

    def public_key(self):
        modulus, exponent = unpad64(self.n), unpad64(self.e)
        if not modulus or not exponent or modulus[0] == 0 or exponent[0] == 0:
            raise ValueError("Noncanonical RSA integers")
        n, e = int.from_bytes(modulus), int.from_bytes(exponent)
        if not 2048 <= n.bit_length() <= 8192 or not 65537 <= e < 2**32 or e % 2 == 0:
            raise ValueError("RSA key outside policy")
        return rsa.RSAPublicNumbers(e, n).public_key()


class PublicKeySet(Contract):
    keys: list[PublicJWK] = Field(min_length=1, max_length=16)


def load_keyset(path: Path):
    """Read each authentication; no stale cache or network fallback after revocation.

    Trusted directory/config mounts may use symlinks, but the opened target must be
    a bounded regular file. Nonblocking open avoids hanging on a mistaken FIFO.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("JWKS must be a regular file")
            raw = stream.read(100_001)
        if len(raw) > 100_000:
            raise ValueError("JWKS exceeds 100 KB")
        manifest = PublicKeySet.model_validate(decode_json(raw.decode("utf-8")))
        if len({key.kid for key in manifest.keys}) != len(manifest.keys):
            raise ValueError("Duplicate trusted key ID")
        return {key.kid: key.public_key() for key in manifest.keys}
    except (OSError, ValueError, TypeError, RecursionError, UnsupportedAlgorithm):
        raise DomainError(
            "AUTH_UNCONFIGURED", "Trusted authentication keys are unavailable or invalid", 503
        ) from None


def token_parts(token: str):
    if not isinstance(token, str) or not 1 <= len(token) <= 16_384:
        raise ValueError("Token size outside policy")
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError("Signed compact JWT required")
    header = decode_json(unpad64(parts[0]).decode("utf-8"))
    claims = decode_json(unpad64(parts[1]).decode("utf-8"))
    unpad64(parts[2])
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise ValueError("JWT JSON objects required")
    # No jku/jwk/x5u/crit/b64 negotiation, including unknown header extensions.
    if set(header) - {"alg", "typ", "kid"}:
        raise ValueError("Unsupported JWT header")
    if "kid" in header and (
        not isinstance(header["kid"], str) or not 1 <= len(header["kid"]) <= 80
    ):
        raise ValueError("Invalid key ID")
    return header, claims
