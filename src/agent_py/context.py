import re
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import or_, select

from agent_py.db import Document, now
from agent_py.domain import DomainError, Principal, digest
from agent_py.security import check_grant


def terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.lower()))


@dataclass(frozen=True)
class ContextBundle:
    documents: list[dict]
    omitted: list[dict]
    estimated_tokens: int
    digest: str
    policy_version: str = "context-v1-lexical"

    def as_dict(self):
        return asdict(self)


class ContextCompiler:
    """ACL filtering precedes ranking. Byte budget is conservative for text-only material."""

    def __init__(self, db):
        self.db = db

    def compile(
        self,
        principal: Principal,
        project: str,
        environment: str,
        query: str,
        budget: int = 6000,
        as_of: datetime | None = None,
    ) -> ContextBundle:
        if budget < 1 or budget > 100_000:
            raise DomainError("CONTEXT_BUDGET", "Invalid context budget", 422)
        if project not in principal.projects or environment not in principal.environments:
            raise DomainError("FORBIDDEN", "Context outside principal scope", 403)
        when = as_of or now()
        with self.db.session(principal.tenant_id) as s:
            check_grant(s, principal.tenant_id, principal.subject, project, environment)
            docs = s.scalars(
                select(Document).where(
                    Document.tenant_id == principal.tenant_id,
                    Document.project == project,
                    Document.revoked.is_(False),
                    Document.valid_from <= when,
                    or_(Document.valid_until.is_(None), Document.valid_until > when),
                )
            ).all()
            allowed = [d for d in docs if principal.subject in d.allowed_subjects]
        q = terms(query)
        ranked = sorted(allowed, key=lambda d: (-len(q & terms(d.body)), d.id))
        included, omitted, used = [], [], 0
        for d in ranked:
            if not q & terms(d.body):
                continue
            entry = {
                "id": d.id,
                "source": d.source,
                "version": d.version,
                "body": d.body,
                "trust": "untrusted_source",
            }
            import json

            cost = len(json.dumps(entry, ensure_ascii=False).encode())
            if used + cost > budget:
                omitted.append({"id": d.id, "reason": "budget"})
                continue
            used += cost
            included.append(entry)
        return ContextBundle(included, omitted, used, digest(included))

    def validate(self, principal: Principal, project: str, environment: str, bundle: ContextBundle):
        with self.db.session(principal.tenant_id) as s:
            check_grant(s, principal.tenant_id, principal.subject, project, environment)
            for item in bundle.documents:
                d = s.scalar(
                    select(Document).where(
                        Document.id == item["id"],
                        Document.tenant_id == principal.tenant_id,
                        Document.project == project,
                        Document.version == item["version"],
                        Document.revoked.is_(False),
                        Document.valid_from <= now(),
                        or_(Document.valid_until.is_(None), Document.valid_until > now()),
                    )
                )
                if (
                    not d
                    or principal.subject not in d.allowed_subjects
                    or digest(d.body) != digest(item["body"])
                ):
                    raise DomainError("EVIDENCE_STALE", "Evidence changed or access revoked", 409)
