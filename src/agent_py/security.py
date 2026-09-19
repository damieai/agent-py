from datetime import UTC, datetime

import jwt
from sqlalchemy import select

from agent_py.auth_keys import load_keyset, token_parts
from agent_py.config import Settings
from agent_py.db import Grant, Task
from agent_py.domain import DomainError, Principal


def issue_dev_token(settings: Settings, principal: Principal, seconds: int | None = None) -> str:
    if settings.environment == "production":
        raise ValueError("Local token issuance is disabled in production")
    if settings.auth_public_key or settings.auth_jwks_file is not None:
        raise ValueError("Local token issuance requires development HS256 authentication")
    seconds = settings.auth_max_token_lifetime_seconds if seconds is None else seconds
    secret = settings.auth_secret.get_secret_value()
    if len(secret) < 32:
        raise ValueError("Configure an authentication secret of at least 32 characters")
    issued = int(datetime.now(UTC).timestamp())
    return jwt.encode(
        {
            **principal.model_dump(),
            "sub": principal.subject,
            "iss": settings.auth_issuer,
            "aud": settings.auth_audience,
            "iat": issued,
            "exp": issued + seconds,
        },
        secret,
        algorithm="HS256",
        headers={"typ": settings.auth_token_type},
    )


def authenticate(settings: Settings, token: str) -> Principal:
    try:
        header, untrusted = token_parts(token)
        rsa_mode = bool(settings.auth_public_key) or settings.auth_jwks_file is not None
        algorithm = "RS256" if rsa_mode else "HS256"
        if header.get("alg") != algorithm or header.get("typ") != settings.auth_token_type:
            raise ValueError("JWT algorithm or type mismatch")
        if settings.auth_jwks_file is not None:
            kid = header.get("kid")
            if not isinstance(kid, str) or not 1 <= len(kid) <= 80:
                raise ValueError("Key ID required")
            key = load_keyset(settings.auth_jwks_file).get(kid)
            if key is None:
                raise ValueError("Untrusted key ID")
        else:
            key = settings.auth_public_key or settings.auth_secret.get_secret_value()
            if not key or (not rsa_mode and len(key) < 32):
                raise DomainError("AUTH_UNCONFIGURED", "Authentication is not configured", 503)
        # PyJWT accepts some coercible timestamps; this API requires integer NumericDates.
        if any(type(untrusted.get(name)) is not int for name in ("iat", "exp")):
            raise ValueError("Integer token times required")
        if "nbf" in untrusted and type(untrusted["nbf"]) is not int:
            raise ValueError("Integer not-before required")
        if not 0 < untrusted["exp"] - untrusted["iat"] <= settings.auth_max_token_lifetime_seconds:
            raise ValueError("Token lifetime outside policy")
        claims = jwt.decode(
            token,
            key,
            algorithms=[algorithm],
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            leeway=settings.auth_clock_skew_seconds,
        )
        principal = Principal(
            tenant_id=claims["tenant_id"],
            subject=claims["sub"],
            roles=claims["roles"],
            projects=claims["projects"],
            environments=claims["environments"],
        )
        if not 1 <= len(principal.tenant_id) <= 120 or not 1 <= len(principal.subject) <= 160:
            raise ValueError("Invalid identity scope")
        for values, maximum in (
            (principal.roles, 80),
            (principal.projects, 80),
            (principal.environments, 40),
        ):
            if len(values) > 100 or any(not 1 <= len(value) <= maximum for value in values):
                raise ValueError("Invalid authorization scope")
        return principal
    except (jwt.PyJWTError, KeyError, ValueError, TypeError, RecursionError):
        raise DomainError("UNAUTHENTICATED", "Invalid or expired credential", 401) from None


def check_grant(s, tenant: str, subject: str, project: str, environment: str):
    grant = s.scalar(
        select(Grant).where(
            Grant.tenant_id == tenant,
            Grant.subject == subject,
            Grant.project == project,
            Grant.environment == environment,
        )
    )
    if grant is None or grant.revoked:
        raise DomainError("PERMISSION_REVOKED", "No active resource grant", 403)


def authorize(s, principal: Principal, task: Task, role: str | None = None):
    c = task.contract
    if (
        task.tenant_id != principal.tenant_id
        or c["project"] not in principal.projects
        or c["environment"] not in principal.environments
    ):
        raise DomainError("FORBIDDEN", "Resource is outside authorized scope", 403)
    if role and role not in principal.roles:
        raise DomainError("FORBIDDEN", f"Role {role} is required", 403)
    check_grant(s, principal.tenant_id, principal.subject, c["project"], c["environment"])
