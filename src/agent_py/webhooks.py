import hashlib
import hmac
import json
import time

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agent_py.db import Inbox, Operation
from agent_py.domain import DomainError


class Notice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=160)
    operation_id: str = Field(pattern=r"^[a-f0-9-]{36}$")


def receive(db, settings, body: bytes, timestamp: str, signature: str) -> str:
    """Signed connector notices are hints, never authoritative business results."""
    key = settings.webhook_secret.get_secret_value()
    tenant = settings.webhook_tenant
    if len(key) < 32 or not tenant:
        raise DomainError("WEBHOOK_UNCONFIGURED", "Connector is not provisioned", 503)
    try:
        if abs(time.time() - int(timestamp)) > 300:
            raise ValueError()
    except ValueError:
        raise DomainError("WEBHOOK_EXPIRED", "Webhook timestamp outside allowed window", 401)
    expected = hmac.new(key.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise DomainError("WEBHOOK_SIGNATURE", "Invalid connector signature", 401)
    try:
        notice = Notice.model_validate(json.loads(body))
    except (ValueError, TypeError):
        raise DomainError("WEBHOOK_SCHEMA", "Invalid connector notice", 422)
    try:
        with db.session(tenant) as s:
            existing = s.scalar(
                select(Inbox).where(
                    Inbox.tenant_id == tenant,
                    Inbox.provider == "connector",
                    Inbox.external_id == notice.event_id,
                )
            )
            if existing:
                if existing.payload != notice.model_dump():
                    raise DomainError(
                        "WEBHOOK_CONFLICT", "Event identity already binds another notice"
                    )
                return existing.external_id
            s.add(
                Inbox(
                    tenant_id=tenant,
                    provider="connector",
                    external_id=notice.event_id,
                    payload=notice.model_dump(),
                )
            )
    except IntegrityError:
        with db.session(tenant) as s:
            existing = s.scalar(
                select(Inbox).where(
                    Inbox.tenant_id == tenant,
                    Inbox.provider == "connector",
                    Inbox.external_id == notice.event_id,
                )
            )
            if not existing or existing.payload != notice.model_dump():
                raise DomainError("WEBHOOK_CONFLICT", "Concurrent event identity conflict")
    return notice.event_id


def consume(service, tenant):
    with service.db.session(tenant) as s:
        notices = list(
            s.scalars(
                select(Inbox)
                .where(Inbox.tenant_id == tenant, Inbox.processed.is_(False))
                .limit(100)
            )
        )
    for notice in notices:
        with service.db.session(tenant) as s:
            op = s.scalar(
                select(Operation).where(
                    Operation.tenant_id == tenant, Operation.id == notice.payload["operation_id"]
                )
            )
        if op:
            service.reconcile(tenant, op.id)
        with service.db.session(tenant) as s:
            row = s.scalar(select(Inbox).where(Inbox.tenant_id == tenant, Inbox.id == notice.id))
            row.processed = True
