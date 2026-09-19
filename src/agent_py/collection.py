"""Operator-configured enterprise evidence collection; no model-selected URLs or credentials."""

import json
import os
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field
from sqlalchemy import select

from agent_py.adapters.enterprise import EnterpriseClient
from agent_py.db import Document, Task, now, tenant_get
from agent_py.domain import Contract, DomainError, Principal, digest
from agent_py.security import authorize


class EvidenceSource(Contract):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    tenant: str
    project: str
    environment: str
    resource: str
    subjects: list[str] = Field(min_length=1)
    provider: Literal["bitbucket_pr", "jira_issue", "jenkins_build", "kubernetes_deployment"]
    base_url: str
    token_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    username_env: str | None = None
    parameters: dict[str, str | int]


class CollectionManifest(Contract):
    sources: list[EvidenceSource] = Field(max_length=20)

    @classmethod
    def load(cls, path: Path):
        with path.open("rb") as stream:
            raw = stream.read(100_001)
        if len(raw) > 100_000:
            raise DomainError("MANIFEST_LIMIT", "Collection manifest exceeds 100 KB")
        manifest = cls.model_validate_json(raw)
        if len({s.name for s in manifest.sources}) != len(manifest.sources):
            raise DomainError("MANIFEST_CONFLICT", "Source names must be unique")
        return manifest


def project_response(provider: str, data: dict):
    required_objects = (
        ("metadata", "spec", "status")
        if provider == "kubernetes_deployment"
        else ("fields",)
        if provider == "jira_issue"
        else ()
    )
    if any(key in data and not isinstance(data[key], dict) for key in required_objects):
        raise DomainError(
            "EVIDENCE_SCHEMA", "Enterprise evidence contains invalid object fields", 502
        )
    # Never ingest entire build parameters or pod specs: they may contain credentials.
    fields = {
        "bitbucket_pr": ("id", "title", "description", "state", "updated_on"),
        "jira_issue": ("id", "key", "fields"),
        "jenkins_build": ("number", "result", "building"),
    }
    if provider == "kubernetes_deployment":
        metadata, spec, status = (
            data.get("metadata", {}),
            data.get("spec", {}),
            data.get("status", {}),
        )
        return {
            "metadata": {
                k: metadata[k]
                for k in ("name", "namespace", "generation", "resourceVersion")
                if k in metadata
            },
            "replicas": spec.get("replicas"),
            "status": {
                k: status[k]
                for k in (
                    "observedGeneration",
                    "replicas",
                    "readyReplicas",
                    "availableReplicas",
                    "updatedReplicas",
                )
                if k in status
            },
        }
    selected = {k: data[k] for k in fields[provider] if k in data}
    if provider == "jira_issue":
        selected["fields"] = {
            k: data.get("fields", {})[k]
            for k in ("summary", "description", "status", "updated")
            if k in data.get("fields", {})
        }
    return selected


class EvidenceCollector:
    def __init__(self, service, manifest: CollectionManifest, client_factory=EnterpriseClient):
        self.service, self.manifest, self.client_factory = service, manifest, client_factory

    def collect(self, principal: Principal, task_id: str) -> list[str]:
        task = self.service.get_task(principal, task_id)
        sources = [
            source
            for source in self.manifest.sources
            if (
                source.tenant == principal.tenant_id
                and source.project == task.contract["project"]
                and source.environment == task.contract["environment"]
                and source.resource == task.contract["resource"]
                and principal.subject in source.subjects
            )
        ]
        if not sources:
            raise DomainError("SOURCE_SCOPE", "No enterprise sources authorized for this task", 403)
        collected = []
        for source in sources:
            from agent_py.resilience import read_with_policy

            def reauthorize():
                with self.service.db.session(principal.tenant_id) as s:
                    current = tenant_get(s, Task, task_id, principal.tenant_id)
                    authorize(s, principal, current)
                    self.service._executable(s, current)

            def read():
                token = os.environ.get(source.token_env, "")
                username = os.environ.get(source.username_env, "") if source.username_env else None
                if not token or (source.username_env and not username):
                    raise DomainError(
                        "CONNECTOR_CREDENTIALS", "Configured connector credentials missing", 503
                    )
                client = self.client_factory(source.base_url, token, username=username)
                try:
                    return getattr(client, source.provider)(**source.parameters)
                finally:
                    client.close()

            data = read_with_policy(self.service, principal, source, read, reauthorize)
            payload = project_response(source.provider, data)
            body = json.dumps(
                {
                    "source": source.name,
                    "provider": source.provider,
                    "locator": {"base_url": source.base_url, "parameters": source.parameters},
                    "data": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if len(body.encode()) > 200_000:
                raise DomainError("EVIDENCE_LIMIT", "Selected enterprise evidence exceeds 200 KB")
            version = digest(payload)
            identity = str(
                uuid5(
                    NAMESPACE_URL,
                    digest(
                        [
                            principal.tenant_id,
                            task_id,
                            source.name,
                            source.base_url,
                            source.parameters,
                            version,
                        ]
                    ),
                )
            )
            with self.service.db.session(principal.tenant_id) as s:
                current = tenant_get(s, Task, task_id, principal.tenant_id)
                authorize(s, principal, current)
                self.service._executable(s, current)
                existing = s.scalar(
                    select(Document).where(
                        Document.id == identity, Document.tenant_id == principal.tenant_id
                    )
                )
                if existing:
                    if existing.revoked or principal.subject not in existing.allowed_subjects:
                        raise DomainError(
                            "EVIDENCE_REVOKED", "Collected evidence has been revoked", 403
                        )
                else:
                    s.add(
                        Document(
                            id=identity,
                            tenant_id=principal.tenant_id,
                            task_id=task_id,
                            project=source.project,
                            source=f"enterprise://{source.provider}/{source.name}/{task_id}",
                            version=version,
                            body=body,
                            allowed_subjects=[principal.subject],
                            valid_from=now(),
                        )
                    )
                collected.append(identity)
        return collected
