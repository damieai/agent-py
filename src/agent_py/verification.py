"""Disposable candidate verification; never executes candidate Python on the host."""

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from agent_py.artifacts import ArtifactStore
from agent_py.db import Task, tenant_get
from agent_py.domain import DomainError, Principal, digest
from agent_py.patches import FileEdit, apply_patch
from agent_py.sandbox import DockerSandbox
from agent_py.security import authorize


def snapshot_python(source: Path, destination: Path) -> dict[str, str]:
    if source.is_symlink() or not source.is_dir():
        raise DomainError("SNAPSHOT_PATH", "Snapshot source must be a real directory")
    manifest, total = {}, 0
    destination.mkdir(parents=True, mode=0o755)
    for index, item in enumerate(source.rglob("*")):
        if index >= 2000:
            raise DomainError("SNAPSHOT_LIMIT", "Snapshot exceeds 2000 directory entries")
        if item.is_symlink():
            raise DomainError("SNAPSHOT_SYMLINK", "Snapshot inputs cannot contain symlinks")
        if item.is_dir():
            continue
        if not item.is_file():
            raise DomainError("SNAPSHOT_SPECIAL", "Snapshot inputs must be regular files")
        if item.suffix != ".py":
            continue
        if len(manifest) >= 200:
            raise DomainError("SNAPSHOT_LIMIT", "Snapshot exceeds 200 Python files")
        with os.fdopen(os.open(item, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            data = stream.read(2_000_001 - total)
        total += len(data)
        if total > 2_000_000:
            raise DomainError("SNAPSHOT_LIMIT", "Snapshot exceeds 2 MB")
        relative = item.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        target.write_bytes(data)
        target.chmod(0o644)
        manifest[relative.as_posix()] = hashlib.sha256(data).hexdigest()
    if not manifest:
        raise DomainError("SNAPSHOT_EMPTY", "No Python files available for verification")
    return manifest


class VerificationRunner:
    def __init__(self, service, sandbox_factory=DockerSandbox):
        self.service, self.sandbox_factory = service, sandbox_factory

    def _verify(self, sandbox, principal, task_id, workspace, oracle, phase):
        from agent_py.observations import stage

        with stage(
            self.service.telemetry,
            "sandbox.verify",
            principal.tenant_id,
            task_id,
            **{"stage.phase": phase},
        ) as span:
            result = sandbox.verify(
                workspace,
                oracle=oracle,
                timeout_seconds=self.service.settings.sandbox_timeout_seconds,
            )
            span.set_attribute("stage.exit_code", result.exit_code)
            span.set_attribute("stage.limit", result.limit or "none")
            return result

    def _check(self, principal, task_id):
        with self.service.db.session(principal.tenant_id) as s:
            task = tenant_get(s, Task, task_id, principal.tenant_id)
            authorize(s, principal, task, "developer")
            self.service._executable(s, task)
            if task.contract["kind"] != "repair":
                raise DomainError(
                    "VERIFICATION_SCOPE", "Candidate verification requires a repair task"
                )

    def run(
        self,
        principal: Principal,
        task_id: str,
        source: Path,
        edits: list[FileEdit],
        *,
        verification_id: str | None = None,
        expected_oracle_digest: str | None = None,
    ):
        self._check(principal, task_id)
        settings = self.service.settings
        if not settings.sandbox_image or settings.sandbox_oracle is None:
            raise DomainError(
                "SANDBOX_UNCONFIGURED", "Configure a pinned image and trusted oracle", 503
            )
        root = settings.sandbox_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        sandbox = self.sandbox_factory(settings.sandbox_image, root)
        if source.is_symlink():
            raise DomainError("SNAPSHOT_SYMLINK", "Source checkout cannot be a symlink")
        oracle = settings.sandbox_oracle.resolve()
        checkout = source.resolve()
        if oracle == checkout or checkout in oracle.parents or oracle in checkout.parents:
            raise DomainError(
                "ORACLE_SCOPE", "Trusted oracle must be outside the candidate checkout"
            )
        with tempfile.TemporaryDirectory(prefix="verification-", dir=root) as temporary:
            work = Path(temporary)
            baseline, candidate, oracle_copy = (
                work / "baseline",
                work / "candidate",
                work / "oracle",
            )
            base_manifest = snapshot_python(source / "src", baseline / "src")
            oracle_manifest = snapshot_python(settings.sandbox_oracle, oracle_copy)
            if (
                expected_oracle_digest is not None
                and digest(oracle_manifest) != expected_oracle_digest
            ):
                raise DomainError("ORACLE_CHANGED", "Oracle changed since repair snapshot")
            shutil.copytree(baseline, candidate)
            apply_patch(candidate, edits)
            candidate_manifest = {
                path: hashlib.sha256((candidate / "src" / path).read_bytes()).hexdigest()
                for path in base_manifest
            }
            self._check(principal, task_id)
            before = self._verify(sandbox, principal, task_id, baseline, oracle_copy, "baseline")
            self._check(principal, task_id)
            after = self._verify(sandbox, principal, task_id, candidate, oracle_copy, "candidate")
            self._check(principal, task_id)
            if (
                before.limit
                or after.limit
                or before.exit_code not in (0, 1)
                or after.exit_code not in (0, 1)
            ):
                outcome = "INCONCLUSIVE"
            elif before.exit_code == 1 and after.exit_code == 0:
                outcome = "REGRESSION_FIXED"
            elif before.exit_code == 0:
                outcome = "BASELINE_NOT_REPRODUCED"
            else:
                outcome = "CANDIDATE_FAILED"
            report = {
                "verification_id": verification_id,
                "outcome": outcome,
                "image": settings.sandbox_image,
                "baseline_manifest": base_manifest,
                "candidate_manifest": candidate_manifest,
                "oracle_manifest": oracle_manifest,
                "oracle_digest": digest(oracle_manifest),
                "baseline": asdict(before),
                "candidate": asdict(after),
                "business_verified": False,
                "limitations": "Python oracle executes in the same process as candidate imports; "
                "this is a regression test, not proof against adversarial oracle manipulation.",
            }
            artifact = ArtifactStore(self.service.db, settings.artifact_root).put(
                principal.tenant_id,
                task_id,
                "candidate-verification",
                json.dumps(report, sort_keys=True).encode(),
            )
            return {"artifact_id": artifact.id, "outcome": outcome}
