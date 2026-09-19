"""Bounded source-repair loop. Produces reviewable candidates; never merges or deploys."""

import difflib
import json
import tempfile
from datetime import timedelta
from pathlib import Path

from pydantic import Field
from sqlalchemy import select, update

from agent_py.artifacts import ArtifactStore
from agent_py.context import ContextBundle, ContextCompiler
from agent_py.db import Artifact, RepairAttempt, RepairRun, Task, emit, now, tenant_get, uid
from agent_py.domain import Contract, DomainError, Principal, digest
from agent_py.model import AnthropicGateway
from agent_py.patches import PatchProposal
from agent_py.security import authorize
from agent_py.service import aware
from agent_py.verification import VerificationRunner, snapshot_python


class RepairBinding(Contract):
    tenant: str
    project: str
    environment: str
    resource: str
    subjects: list[str] = Field(min_length=1)
    source_root: str
    allow_model_export: bool = False
    max_attempts: int = Field(default=2, ge=1, le=3)


class RepairManifest(Contract):
    repositories: list[RepairBinding] = Field(min_length=1, max_length=30)


def insert_once(s, model, values, keys):
    if s.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    s.execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=keys))


def repair_details(service, principal, task_id):
    task = service.get_task(principal, task_id)
    if principal.subject != task.principal and "operator" not in principal.roles:
        raise DomainError(
            "REPAIR_REVIEW_FORBIDDEN", "Repair review requires task ownership or operator role", 403
        )
    with service.db.session(principal.tenant_id) as s:
        run = s.scalar(
            select(RepairRun).where(
                RepairRun.tenant_id == principal.tenant_id, RepairRun.task_id == task_id
            )
        )
        if not run:
            return None
        attempts = s.scalars(
            select(RepairAttempt)
            .where(RepairAttempt.tenant_id == principal.tenant_id, RepairAttempt.run_id == run.id)
            .order_by(RepairAttempt.ordinal)
        ).all()
        return {
            "id": run.id,
            "state": run.state,
            "max_attempts": run.max_attempts,
            "source_digest": run.source_digest,
            "snapshot_id": run.snapshot_id,
            "attempts": [
                {
                    "id": a.id,
                    "ordinal": a.ordinal,
                    "state": a.state,
                    "summary": a.proposal.get("summary") if a.proposal else None,
                    "outcome": a.outcome,
                    "patch_artifact_id": a.patch_artifact_id,
                    "verification_artifact_id": a.verification_artifact_id,
                    "verification_count": a.verification_count,
                }
                for a in attempts
            ],
        }


class RepairHarness:
    def __init__(self, service, gateway=None, verifier=None):
        self.service, self.gateway = service, gateway
        self.verifier = verifier or VerificationRunner(service)
        self.store = ArtifactStore(service.db, service.settings.artifact_root)

    def _principal(self, task):
        return Principal(
            tenant_id=task.tenant_id,
            subject=task.principal,
            roles=["developer"],
            projects=[task.contract["project"]],
            environments=[task.contract["environment"]],
        )

    def _check(self, principal, task_id):
        with self.service.db.session(principal.tenant_id) as s:
            task = tenant_get(s, Task, task_id, principal.tenant_id)
            authorize(s, principal, task, "developer")
            self.service._executable(s, task)
            return task

    def _binding(self, task):
        settings = self.service.settings
        if not settings.allow_model_api or not settings.allow_candidate_execution:
            raise DomainError(
                "REPAIR_DISABLED",
                "Repair requires explicit model and candidate-execution opt-in",
                403,
            )
        if (
            settings.repair_manifest is None
            or not settings.sandbox_image
            or settings.sandbox_oracle is None
        ):
            raise DomainError(
                "REPAIR_UNCONFIGURED",
                "Configure repository binding, pinned image and trusted oracle",
                503,
            )
        try:
            with settings.repair_manifest.open("rb") as stream:
                raw = stream.read(100_001)
            if len(raw) > 100_000:
                raise DomainError("REPAIR_MANIFEST_LIMIT", "Repository manifest exceeds 100 KB")
            bindings = RepairManifest.model_validate_json(raw).repositories
        except (OSError, ValueError) as exc:
            raise DomainError(
                "REPAIR_UNCONFIGURED", "Repository manifest is unavailable or invalid", 503
            ) from exc
        matches = [
            b
            for b in bindings
            if b.tenant == task.tenant_id
            and b.project == task.contract["project"]
            and b.environment == task.contract["environment"]
            and b.resource == task.contract["resource"]
            and task.principal in b.subjects
        ]
        if len(matches) != 1 or not matches[0].allow_model_export:
            raise DomainError(
                "REPAIR_SOURCE_SCOPE",
                "Exactly one export-authorized repository binding is required",
                403,
            )
        return matches[0]

    def _source(self, binding):
        root = Path(binding.source_root)
        if not root.is_absolute() or root.is_symlink():
            raise DomainError(
                "REPAIR_SOURCE_SCOPE", "Repository root must be an absolute non-symlink path"
            )
        with tempfile.TemporaryDirectory(prefix="agent-source-") as temp:
            copy = Path(temp) / "src"
            manifest = snapshot_python(root / "src", copy)
            if len(manifest) > 20:
                raise DomainError(
                    "REPAIR_SOURCE_LIMIT", "Repair input supports at most 20 Python files"
                )
            sources = {
                "src/" + name: (copy / name).read_bytes().decode("utf-8")
                for name in sorted(manifest)
            }
            if sum(len(body.encode()) for body in sources.values()) > 64_000:
                raise DomainError("REPAIR_SOURCE_LIMIT", "Repair source input exceeds 64 KB")
            return sources, {"src/" + name: sha for name, sha in manifest.items()}

    def _configuration(self, binding):
        settings = self.service.settings
        from agent_py.sandbox import DockerSandbox

        try:
            DockerSandbox(settings.sandbox_image, settings.sandbox_root)
        except ValueError as exc:
            raise DomainError(
                "REPAIR_UNCONFIGURED", "Repair requires a valid digest-pinned image", 503
            ) from exc
        root, oracle_root = Path(binding.source_root).resolve(), settings.sandbox_oracle.resolve()
        if root == oracle_root or root in oracle_root.parents or oracle_root in root.parents:
            raise DomainError(
                "ORACLE_SCOPE", "Trusted oracle must be outside the candidate checkout"
            )
        with tempfile.TemporaryDirectory(prefix="agent-oracle-") as temp:
            oracle = snapshot_python(settings.sandbox_oracle, Path(temp) / "oracle")
        oracle_digest = digest(oracle)
        config = digest(
            {
                "binding": binding.model_dump(),
                "oracle": oracle_digest,
                "image": settings.sandbox_image,
                "timeout": settings.sandbox_timeout_seconds,
                "model": settings.model_id,
                "input_price": settings.model_input_micro_per_token,
                "output_price": settings.model_output_micro_per_token,
            }
        )
        return config, oracle_digest

    def _wait(self, principal, task_id, reason):
        with self.service.db.session(principal.tenant_id) as s:
            task = tenant_get(s, Task, task_id, principal.tenant_id, True)
            if not task.cancelled and not task.taken_over and task.status != "TERMINATED":
                if task.waiting_reason != reason:
                    task.status, task.waiting_reason = "WAITING", reason
                    emit(s, task, "repair.waiting", {"reason": reason})
        return {"done": False, "wait": reason}

    def tick(self, tenant, task_id):
        with self.service.db.session(tenant) as s:
            task = tenant_get(s, Task, task_id, tenant)
        if task.status == "TERMINATED":
            return {"done": True, "result": task.result}
        if task.cancelled:
            self.service.finish(tenant, task_id)
            return {"done": True, "result": "CANCELLED"}
        if task.taken_over:
            return {"done": False, "wait": "HUMAN_TAKEOVER"}
        principal = self._principal(task)
        self._check(principal, task_id)
        binding = self._binding(task)
        with self.service.db.session(tenant) as s:
            s.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.tenant_id == tenant,
                    Task.cancelled.is_(False),
                    Task.taken_over.is_(False),
                    Task.status == "QUEUED",
                )
                .values(status="RUNNING", waiting_reason=None)
            )
        config_digest, oracle_digest = self._configuration(binding)
        sources, source_manifest = self._source(binding)
        with self.service.db.session(tenant) as s:
            run = s.scalar(
                select(RepairRun).where(RepairRun.tenant_id == tenant, RepairRun.task_id == task_id)
            )
        compiler = ContextCompiler(self.service.db)
        if run is None:
            if self.service.settings.collection_manifest is not None:
                from agent_py.collection import CollectionManifest, EvidenceCollector

                EvidenceCollector(
                    self.service, CollectionManifest.load(self.service.settings.collection_manifest)
                ).collect(principal, task_id)
            bundle = compiler.compile(
                principal,
                binding.project,
                binding.environment,
                task.contract["goal"],
                task_id=task_id,
            )
            snapshot = {
                "sources": sources,
                "source_sha256": source_manifest,
                "context": bundle.as_dict(),
                "oracle_digest": oracle_digest,
            }
            artifact = self.store.put(
                tenant, task_id, "repair-input", json.dumps(snapshot, sort_keys=True).encode()
            )
            self._check(principal, task_id)
            with self.service.db.session(tenant) as s:
                insert_once(
                    s,
                    RepairRun,
                    {
                        "id": uid(),
                        "tenant_id": tenant,
                        "task_id": task_id,
                        "snapshot_id": artifact.id,
                        "source_digest": digest(source_manifest),
                        "config_digest": config_digest,
                        "max_attempts": binding.max_attempts,
                        "state": "ACTIVE",
                        "created_at": now(),
                    },
                    ["tenant_id", "task_id"],
                )
            return {"done": False, "phase": "SNAPSHOT_SAVED"}
        if run.config_digest != config_digest or run.source_digest != digest(source_manifest):
            with self.service.db.session(tenant) as s:
                s.execute(
                    update(RepairRun)
                    .where(RepairRun.id == run.id, RepairRun.tenant_id == tenant)
                    .values(state="REPAIR_INPUT_CHANGED")
                )
            raise DomainError(
                "REPAIR_INPUT_CHANGED",
                "Source, oracle or repair configuration changed; create a new task",
            )
        _, raw = self.store.read(principal, run.snapshot_id)
        snapshot = json.loads(raw)
        bundle = ContextBundle(**snapshot["context"])
        try:
            compiler.validate(
                principal, binding.project, binding.environment, bundle, task_id=task_id
            )
        except DomainError:
            with self.service.db.session(tenant) as s:
                s.execute(
                    update(RepairRun)
                    .where(RepairRun.id == run.id, RepairRun.tenant_id == tenant)
                    .values(state="REPAIR_EVIDENCE_CHANGED")
                )
            raise
        if run.state != "ACTIVE":
            return self._wait(principal, task_id, run.state)
        with self.service.db.session(tenant) as s:
            attempts = list(
                s.scalars(
                    select(RepairAttempt)
                    .where(RepairAttempt.tenant_id == tenant, RepairAttempt.run_id == run.id)
                    .order_by(RepairAttempt.ordinal)
                )
            )
        attempt = attempts[-1] if attempts else None
        if attempt and attempt.state == "VERIFIED" and attempt.outcome != "CANDIDATE_FAILED":
            return self._end(
                principal,
                task_id,
                run.id,
                "CANDIDATE_READY_FOR_REVIEW"
                if attempt.outcome == "REGRESSION_FIXED"
                else "REPAIR_INCONCLUSIVE",
            )
        if attempt is None or (
            attempt.state == "VERIFIED" and attempt.outcome == "CANDIDATE_FAILED"
        ):
            ordinal = 1 if attempt is None else attempt.ordinal + 1
            if ordinal > run.max_attempts:
                return self._end(principal, task_id, run.id, "REPAIR_EXHAUSTED")
            with self.service.db.session(tenant) as s:
                insert_once(
                    s,
                    RepairAttempt,
                    {
                        "id": uid(),
                        "tenant_id": tenant,
                        "run_id": run.id,
                        "ordinal": ordinal,
                        "state": "GENERATING",
                        "verification_count": 0,
                        "created_at": now(),
                    },
                    ["tenant_id", "run_id", "ordinal"],
                )
            return {"done": False, "phase": "ATTEMPT_CREATED", "ordinal": ordinal}
        if attempt.state == "BLOCKED":
            recovered = self._recover_report(principal, task_id, attempt.verification_token)
            if recovered and attempt.outcome == "VERIFICATION_INTERRUPTED":
                with self.service.db.session(tenant) as s:
                    changed = s.execute(
                        update(RepairAttempt)
                        .where(
                            RepairAttempt.id == attempt.id,
                            RepairAttempt.tenant_id == tenant,
                            RepairAttempt.state == "BLOCKED",
                            RepairAttempt.verification_token == attempt.verification_token,
                        )
                        .values(state="VERIFYING")
                    )
                if changed.rowcount:
                    return self._verified(principal, task_id, run, attempt, recovered)
            return self._wait(principal, task_id, attempt.outcome or "VERIFICATION_INTERRUPTED")
        if attempt.state == "GENERATING":
            context = {
                "documents": bundle.documents,
                "sources": snapshot["sources"],
                "source_sha256": snapshot["source_sha256"],
                "feedback": [
                    {"attempt": a.ordinal, "outcome": a.outcome}
                    for a in attempts
                    if a.state == "VERIFIED"
                ],
            }
            gateway = self.gateway or AnthropicGateway(
                self.service.settings,
                self.service,
                self.service.settings.model_input_micro_per_token,
                self.service.settings.model_output_micro_per_token,
            )
            proposal = gateway.propose_patch(
                tenant,
                task_id,
                f"repair:{run.id}:{attempt.ordinal}",
                task.contract["goal"],
                context,
            )
            self._check(principal, task_id)
            compiler.validate(
                principal, binding.project, binding.environment, bundle, task_id=task_id
            )
            diff = "".join(
                "".join(
                    difflib.unified_diff(
                        snapshot["sources"][e.path].splitlines(True),
                        e.content.splitlines(True),
                        fromfile="a/" + e.path,
                        tofile="b/" + e.path,
                    )
                )
                for e in proposal.edits
            )
            artifact = self.store.put(
                tenant,
                task_id,
                "repair-patch",
                json.dumps(
                    {
                        "proposal": proposal.model_dump(),
                        "diff": diff,
                        "source_digest": run.source_digest,
                    },
                    sort_keys=True,
                ).encode(),
            )
            with self.service.db.session(tenant) as s:
                changed = s.execute(
                    update(RepairAttempt)
                    .where(
                        RepairAttempt.id == attempt.id,
                        RepairAttempt.tenant_id == tenant,
                        RepairAttempt.state == "GENERATING",
                    )
                    .values(
                        state="GENERATED",
                        proposal=proposal.model_dump(),
                        patch_artifact_id=artifact.id,
                    )
                )
                if changed.rowcount:
                    emit(
                        s,
                        tenant_get(s, Task, task_id, tenant),
                        "repair.patch_generated",
                        {"attempt_id": attempt.id, "artifact_id": artifact.id},
                    )
            return {"done": False, "phase": "PATCH_GENERATED"}
        if attempt.state == "VERIFYING":
            recovered = self._recover_report(principal, task_id, attempt.verification_token)
            if recovered:
                return self._verified(principal, task_id, run, attempt, recovered)
            if (
                aware(attempt.verification_started_at)
                + timedelta(seconds=2 * self.service.settings.sandbox_timeout_seconds + 30)
                < now()
            ):
                self._block_verification(tenant, attempt, "VERIFICATION_INTERRUPTED")
            return self._wait(principal, task_id, "VERIFICATION_PENDING")
        if attempt.state == "GENERATED":
            token = uid()
            with self.service.db.session(tenant) as s:
                changed = s.execute(
                    update(RepairAttempt)
                    .where(
                        RepairAttempt.id == attempt.id,
                        RepairAttempt.tenant_id == tenant,
                        RepairAttempt.state == "GENERATED",
                        RepairAttempt.verification_count < 3,
                    )
                    .values(
                        state="VERIFYING",
                        verification_token=token,
                        verification_started_at=now(),
                        verification_count=RepairAttempt.verification_count + 1,
                    )
                )
                if changed.rowcount != 1:
                    return {"done": False, "wait": "VERIFICATION_PENDING"}
            attempt.verification_token = token
            try:
                with tempfile.TemporaryDirectory(prefix="agent-repair-") as temp:
                    root = Path(temp)
                    for name, body in snapshot["sources"].items():
                        target = root / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(body.encode("utf-8"))
                    result = self.verifier.run(
                        principal,
                        task_id,
                        root,
                        PatchProposal.model_validate(attempt.proposal).edits,
                        verification_id=token,
                        expected_oracle_digest=oracle_digest,
                    )
                return self._verified(principal, task_id, run, attempt, result)
            except Exception:
                # Persist uncertainty; retrying the Activity must not launch another container.
                self._block_verification(tenant, attempt, "VERIFICATION_INTERRUPTED")
                raise
        raise DomainError("REPAIR_STATE", "Unexpected repair state")

    def _recover_report(self, principal, task_id, token):
        if token is None:
            return None
        with self.service.db.session(principal.tenant_id) as s:
            artifacts = list(
                s.scalars(
                    select(Artifact).where(
                        Artifact.tenant_id == principal.tenant_id,
                        Artifact.task_id == task_id,
                        Artifact.kind == "candidate-verification",
                    )
                )
            )
        for artifact in artifacts:
            _, raw = self.store.read(principal, artifact.id)
            report = json.loads(raw)
            if report.get("verification_id") == token:
                return {"artifact_id": artifact.id, "outcome": report["outcome"]}
        return None

    def _block_verification(self, tenant, attempt, reason):
        with self.service.db.session(tenant) as s:
            s.execute(
                update(RepairAttempt)
                .where(
                    RepairAttempt.id == attempt.id,
                    RepairAttempt.tenant_id == tenant,
                    RepairAttempt.state == "VERIFYING",
                    RepairAttempt.verification_token == attempt.verification_token,
                )
                .values(state="BLOCKED", outcome=reason)
            )

    def _verified(self, principal, task_id, run, attempt, result):
        artifact, raw = self.store.read(principal, result["artifact_id"])
        report = json.loads(raw)
        _, snapshot_raw = self.store.read(principal, run.snapshot_id)
        snapshot = json.loads(snapshot_raw)
        expected_baseline = {
            name.removeprefix("src/"): sha for name, sha in snapshot["source_sha256"].items()
        }
        if (
            report.get("verification_id") != attempt.verification_token
            or report.get("outcome") != result["outcome"]
            or artifact.task_id != task_id
            or artifact.kind != "candidate-verification"
            or report.get("baseline_manifest") != expected_baseline
            or report.get("oracle_digest") != snapshot["oracle_digest"]
            or report.get("image") != self.service.settings.sandbox_image
        ):
            raise DomainError(
                "VERIFICATION_MISMATCH", "Verification receipt does not match this attempt"
            )
        with self.service.db.session(principal.tenant_id) as s:
            changed = s.execute(
                update(RepairAttempt)
                .where(
                    RepairAttempt.id == attempt.id,
                    RepairAttempt.tenant_id == principal.tenant_id,
                    RepairAttempt.state == "VERIFYING",
                    RepairAttempt.verification_token == attempt.verification_token,
                )
                .values(
                    state="VERIFIED",
                    outcome=result["outcome"],
                    verification_artifact_id=result["artifact_id"],
                )
            )
            if not changed.rowcount:
                return {"done": False, "wait": "VERIFICATION_SUPERSEDED"}
            emit(
                s,
                tenant_get(s, Task, task_id, principal.tenant_id),
                "repair.verified",
                {"attempt_id": attempt.id, **result},
            )
        if result["outcome"] == "REGRESSION_FIXED":
            return self._end(principal, task_id, run.id, "CANDIDATE_READY_FOR_REVIEW")
        if result["outcome"] != "CANDIDATE_FAILED":
            return self._end(principal, task_id, run.id, "REPAIR_INCONCLUSIVE")
        return {"done": False, "phase": "CANDIDATE_FAILED"}

    def _end(self, principal, task_id, run_id, state):
        with self.service.db.session(principal.tenant_id) as s:
            s.execute(
                update(RepairRun)
                .where(
                    RepairRun.id == run_id,
                    RepairRun.tenant_id == principal.tenant_id,
                    RepairRun.state == "ACTIVE",
                )
                .values(state=state)
            )
            state = tenant_get(s, RepairRun, run_id, principal.tenant_id).state
        return self._wait(principal, task_id, state)

    def retry_verification(self, principal, task_id, attempt_id):
        with self.service.db.session(principal.tenant_id) as s:
            task = tenant_get(s, Task, task_id, principal.tenant_id, True)
            authorize(s, principal, task, "operator")
            self.service._executable(s, task)
            attempt = tenant_get(s, RepairAttempt, attempt_id, principal.tenant_id, True)
            run = tenant_get(s, RepairRun, attempt.run_id, principal.tenant_id)
            if run.task_id != task_id or run.state != "ACTIVE":
                raise DomainError(
                    "REPAIR_SCOPE", "Attempt does not belong to this active repair task"
                )
            changed = s.execute(
                update(RepairAttempt)
                .where(
                    RepairAttempt.id == attempt_id,
                    RepairAttempt.tenant_id == principal.tenant_id,
                    RepairAttempt.state == "BLOCKED",
                    RepairAttempt.outcome == "VERIFICATION_INTERRUPTED",
                    RepairAttempt.verification_count < 3,
                )
                .values(
                    state="GENERATED",
                    outcome=None,
                    verification_token=None,
                    verification_started_at=None,
                )
            )
            if changed.rowcount != 1:
                raise DomainError(
                    "VERIFICATION_RETRY_DENIED",
                    "Only interrupted verification can retry, at most three executions",
                )
            emit(
                s,
                task,
                "repair.verification_retry",
                {"attempt_id": attempt_id, "actor": principal.subject},
            )
        return {"accepted": True, "attempt_id": attempt_id}
