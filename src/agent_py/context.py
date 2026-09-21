import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import or_, select

from agent_py.db import Document, Task, now, tenant_get
from agent_py.domain import DomainError, Principal, digest
from agent_py.security import authorize, check_grant


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

    def __init__(self, db, strategy="lexical", *, telemetry=None):
        if strategy not in {"lexical", "bm25_rrf"}:
            raise DomainError("CONTEXT_STRATEGY", "Unknown retrieval strategy", 422)
        self.telemetry = telemetry
        self.db = db
        self.strategy = strategy

    def _scope(self, s, principal, project, environment, task_id):
        if project not in principal.projects or environment not in principal.environments:
            raise DomainError("FORBIDDEN", "Context outside principal scope", 403)
        check_grant(s, principal.tenant_id, principal.subject, project, environment)
        if task_id is not None:
            task = tenant_get(s, Task, task_id, principal.tenant_id)
            authorize(s, principal, task)
            if task.contract["project"] != project or task.contract["environment"] != environment:
                raise DomainError("CONTEXT_SCOPE", "Context does not match task scope", 403)

    def compile(
        self,
        principal: Principal,
        project: str,
        environment: str,
        query: str,
        budget: int = 6000,
        as_of: datetime | None = None,
        task_id: str | None = None,
    ) -> ContextBundle:
        from agent_py.observations import stage

        with stage(
            self.telemetry,
            "retrieval.compile",
            principal.tenant_id,
            task_id,
            **{"stage.strategy": self.strategy},
        ) as span:
            bundle = self._compile(principal, project, environment, query, budget, as_of, task_id)
            span.set_attribute("stage.document_count", len(bundle.documents))
            span.set_attribute("stage.omitted_count", len(bundle.omitted))
            span.set_attribute("stage.context_bytes", bundle.estimated_tokens)
            span.set_attribute("stage.context_digest", bundle.digest)
            return bundle

    def _compile(
        self,
        principal: Principal,
        project: str,
        environment: str,
        query: str,
        budget: int = 6000,
        as_of: datetime | None = None,
        task_id: str | None = None,
    ) -> ContextBundle:
        if budget < 1 or budget > 100_000:
            raise DomainError("CONTEXT_BUDGET", "Invalid context budget", 422)
        if len(query) > 20_000:
            raise DomainError("CONTEXT_QUERY", "Query exceeds 20000 characters", 422)
        if project not in principal.projects or environment not in principal.environments:
            raise DomainError("FORBIDDEN", "Context outside principal scope", 403)
        when = as_of or now()
        with self.db.session(principal.tenant_id) as s:
            self._scope(s, principal, project, environment, task_id)
            docs = s.scalars(
                select(Document)
                .where(
                    Document.tenant_id == principal.tenant_id,
                    Document.project == project,
                    or_(Document.task_id.is_(None), Document.task_id == task_id),
                    Document.revoked.is_(False),
                    Document.valid_from <= when,
                    or_(Document.valid_until.is_(None), Document.valid_until > when),
                )
                .execution_options(yield_per=100)
            )
            allowed, corpus_bytes = [], 0
            for d in docs:
                if principal.subject not in d.allowed_subjects:
                    continue
                corpus_bytes += len(d.body.encode())
                if len(allowed) >= 1000 or corpus_bytes > 5_000_000:
                    raise DomainError("CONTEXT_CAPACITY", "Authorized corpus exceeds limit", 413)
                allowed.append(d)
        if self.strategy == "bm25_rrf":
            from agent_py.retrieval import POLICY, rank_chunks

            included, omitted, used = [], [], 0
            for entry in rank_chunks(allowed, query):
                cost = len(json.dumps(entry, ensure_ascii=False).encode())
                if used + cost > budget:
                    omitted.append(
                        {"id": entry["id"], "chunk_id": entry["chunk_id"], "reason": "budget"}
                    )
                    continue
                included.append(entry)
                used += cost
            return ContextBundle(included, omitted, used, digest(included), POLICY)
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
            cost = len(json.dumps(entry, ensure_ascii=False).encode())
            if used + cost > budget:
                omitted.append({"id": d.id, "reason": "budget"})
                continue
            used += cost
            included.append(entry)
        return ContextBundle(included, omitted, used, digest(included))

    def validate(
        self,
        principal: Principal,
        project: str,
        environment: str,
        bundle: ContextBundle,
        task_id: str | None = None,
    ):
        from agent_py.retrieval import POLICY, chunk_document

        if bundle.policy_version not in {"context-v1-lexical", POLICY}:
            raise DomainError("CONTEXT_POLICY", "Unsupported persisted context policy", 409)
        if digest(bundle.documents) != bundle.digest:
            raise DomainError("EVIDENCE_STALE", "Context manifest changed", 409)
        with self.db.session(principal.tenant_id) as s:
            self._scope(s, principal, project, environment, task_id)
            checked = {}
            for item in bundle.documents:
                d = s.scalar(
                    select(Document).where(
                        Document.id == item["id"],
                        Document.tenant_id == principal.tenant_id,
                        Document.project == project,
                        or_(Document.task_id.is_(None), Document.task_id == task_id),
                        Document.version == item["version"],
                        Document.revoked.is_(False),
                        Document.valid_from <= now(),
                        or_(Document.valid_until.is_(None), Document.valid_until > now()),
                    )
                )
                if (
                    not d
                    or principal.subject not in d.allowed_subjects
                    or d.source != item["source"]
                    or item.get("trust") != "untrusted_source"
                ):
                    raise DomainError("EVIDENCE_STALE", "Evidence changed or access revoked", 409)
                if bundle.policy_version == POLICY:
                    if d.id not in checked:
                        checked[d.id] = {c["chunk_id"]: c for c in chunk_document(d)}
                    original = checked[d.id].get(item.get("chunk_id"))
                    valid = original and all(item.get(k) == v for k, v in original.items())
                else:
                    valid = digest(d.body) == digest(item["body"])
                if not valid:
                    raise DomainError("EVIDENCE_STALE", "Evidence changed or access revoked", 409)
