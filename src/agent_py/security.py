from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import select

from agent_py.config import Settings
from agent_py.db import Grant, Task
from agent_py.domain import DomainError, Principal


def issue_dev_token(settings: Settings, principal: Principal, seconds: int = 3600) -> str:
    if settings.environment == "production":
        raise ValueError("Local token issuance is disabled in production")
    secret = settings.auth_secret.get_secret_value()
    if len(secret) < 32:
        raise ValueError("Configure an authentication secret of at least 32 characters")
    return jwt.encode(
        {
            **principal.model_dump(),
            "sub": principal.subject,
            "iss": settings.auth_issuer,
            "aud": settings.auth_audience,
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(seconds=seconds),
        },
        secret,
        algorithm="HS256",
    )


def authenticate(settings: Settings, token: str) -> Principal:
    key = settings.auth_public_key or settings.auth_secret.get_secret_value()
    if not key or (not settings.auth_public_key and len(key) < 32):
        raise DomainError("AUTH_UNCONFIGURED", "Authentication is not configured", 503)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256" if settings.auth_public_key else "HS256"],
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
        return Principal(
            tenant_id=claims["tenant_id"],
            subject=claims["sub"],
            roles=claims["roles"],
            projects=claims["projects"],
            environments=claims["environments"],
        )
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise DomainError("UNAUTHENTICATED", "Invalid or expired credential", 401) from exc


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
