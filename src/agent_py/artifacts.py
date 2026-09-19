import hashlib
import os
import tempfile
from pathlib import Path

from sqlalchemy import select

from agent_py.db import Artifact, Task, tenant_get, uid
from agent_py.domain import DomainError, Principal
from agent_py.security import authorize


class ArtifactStore:
    def __init__(self, db, root: Path):
        self.db, self.root = db, root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, tenant: str, task_id: str, kind: str, data: bytes) -> Artifact:
        if len(data) > 10_000_000:
            raise DomainError("ARTIFACT_TOO_LARGE", "Artifact exceeds 10 MB", 413)
        checksum = hashlib.sha256(data).hexdigest()
        with self.db.session(tenant) as s:
            tenant_get(s, Task, task_id, tenant)
            existing = s.scalar(
                select(Artifact).where(
                    Artifact.tenant_id == tenant,
                    Artifact.task_id == task_id,
                    Artifact.kind == kind,
                    Artifact.digest == checksum,
                )
            )
            if existing:
                return existing
            key = uid()
            fd, temporary = tempfile.mkstemp(dir=self.root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.root / key)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            a = Artifact(
                id=uid(),
                tenant_id=tenant,
                task_id=task_id,
                kind=kind,
                digest=checksum,
                storage_key=key,
            )
            s.add(a)
            return a

    def read(self, principal: Principal, artifact_id: str) -> tuple[Artifact, bytes]:
        with self.db.session(principal.tenant_id) as s:
            a = tenant_get(s, Artifact, artifact_id, principal.tenant_id)
            t = tenant_get(s, Task, a.task_id, principal.tenant_id)
            authorize(s, principal, t)
            if (
                t.contract.get("workflow") == "repair_candidate"
                and principal.subject != t.principal
                and "operator" not in principal.roles
            ):
                raise DomainError(
                    "REPAIR_REVIEW_FORBIDDEN",
                    "Repair artifacts require task ownership or operator role",
                    403,
                )
        location = self.root / a.storage_key
        if location.parent.resolve() != self.root or location.is_symlink():
            raise DomainError("ARTIFACT_PATH", "Invalid artifact path", 403)
        fd = os.open(location, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(10_000_001)
        if hashlib.sha256(data).hexdigest() != a.digest:
            raise DomainError("ARTIFACT_CORRUPTED", "Artifact checksum mismatch", 500)
        return a, data
