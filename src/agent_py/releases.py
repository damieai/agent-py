"""Operator-pinned releases: local reproducibility, not provenance or deployment approval."""

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from agent_py.domain import Contract, DomainError, digest
from agent_py.evaluation_gate import evaluate_gate
from agent_py.jsonio import decode_json

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
MAX_FILE = 10_000_000


class RuntimePolicy(Contract):
    execution_mode: Literal["simulation", "live"]
    model_id: str = Field(max_length=200)
    model_input_micro_per_token: int = Field(ge=0)
    model_output_micro_per_token: int = Field(ge=0)
    context_strategy: Literal["lexical", "bm25_rrf"]
    task_queue: str = Field(min_length=1, max_length=200)
    sandbox_image: str = Field(max_length=500)
    sandbox_timeout_seconds: int = Field(ge=1, le=120)


class GateEvidence(Contract):
    policy: dict
    simulation: dict
    baseline: dict
    candidate: dict
    fixture: dict


class AgentRelease(Contract):
    schema_version: Literal["agent-release-v1"] = Field(alias="schema")
    files: dict[str, Hash] = Field(min_length=1, max_length=1000)
    runtime: RuntimePolicy
    bindings: dict[str, Hash | None]
    evidence: GateEvidence
    gate_digest: Hash


def fail(message="Release inputs do not match the pinned runtime"):
    raise DomainError("RELEASE_MISMATCH", message, 409)


def read_regular(path: Path, limit=MAX_FILE):
    # Refuse FIFOs/devices and final symlinks before reading; operator owns parent directories.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Release input must be a regular file")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Release input exceeds size limit")
    return raw


def hash_file(path):
    return hashlib.sha256(read_regular(path)).hexdigest()


def tree_files(root: Path, suffix: str):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Release source directory is unavailable")
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlinks are not allowed in release source trees")
        if path.suffix == suffix and path.is_file():
            if len(found) >= 1000:
                raise ValueError("Release source tree exceeds file limit")
            found[path.relative_to(root).as_posix()] = hash_file(path)
    if not found:
        raise ValueError("Release source tree is empty")
    return found


def source_files(root):
    files = {name: hash_file(root / name) for name in ("pyproject.toml", "uv.lock")}
    for directory in ("src/agent_py", "migrations"):
        files.update(
            {f"{directory}/{k}": v for k, v in tree_files(root / directory, ".py").items()}
        )
    return files


def runtime_policy(settings):
    return RuntimePolicy.model_validate(
        {name: getattr(settings, name) for name in RuntimePolicy.model_fields}
    )


def bindings(settings):
    result = {}
    for name in ("collection_manifest", "repair_manifest"):
        path = getattr(settings, name)
        result[name] = hash_file(path) if path is not None else None
    result["sandbox_oracle"] = (
        digest(tree_files(settings.sandbox_oracle, ".py"))
        if settings.sandbox_oracle is not None
        else None
    )
    return result


def check_evidence(release):
    report = evaluate_gate(**{f"{k}_data": v for k, v in release.evidence.model_dump().items()})
    if report["body"]["status"] != "PASS" or report["digest"] != release.gate_digest:
        fail("Release evaluation evidence does not recompute to PASS")
    if report["body"]["strategies"]["candidate"] != release.runtime.context_strategy:
        fail("Release context strategy differs from the evaluated candidate")


def create_release(settings, root, evidence):
    report = evaluate_gate(**{f"{k}_data": v for k, v in evidence.model_dump().items()})
    release = AgentRelease.model_validate(
        {
            "schema": "agent-release-v1",
            "files": source_files(root),
            "runtime": runtime_policy(settings).model_dump(),
            "bindings": bindings(settings),
            "evidence": evidence.model_dump(),
            "gate_digest": report["digest"],
        }
    )
    check_evidence(release)
    return release


def release_id(release):
    return "sha256:" + digest(release.model_dump(by_alias=True))


def write_release(release, path):
    data = (json.dumps(release.model_dump(by_alias=True), sort_keys=True, indent=2) + "\n").encode()
    if len(data) > MAX_FILE:
        raise ValueError("Release exceeds size limit")
    # Never overwrite an existing release. A partial interrupted file cannot pass its digest.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class ReleaseGuard:
    def __init__(self, settings):
        self.settings = settings
        self.id = "agent-v1"
        self.release = None
        if settings.release_manifest is None:
            return
        try:
            self.release = AgentRelease.model_validate(
                decode_json(read_regular(settings.release_manifest))
            )
            self.id = release_id(self.release)
            if self.id != settings.release_expected_id:
                fail("Release identity differs from the independent deployment pin")
            root = settings.release_root.resolve()
            # Do not validate a different checkout from the imported application package.
            if (root / "src/agent_py").resolve() != Path(__file__).resolve().parent:
                fail("Release root is not the running source package")
            if source_files(root) != self.release.files:
                fail("Release source or dependency lock differs from the running checkout")
            check_evidence(self.release)
            self.check()
        except (OSError, ValueError, TypeError, RecursionError):
            fail("Release is missing, malformed or has invalid local inputs")

    def check(self, task_release=None):
        if task_release is not None and task_release != self.id:
            fail("Task belongs to another release; use its matching Worker")
        if self.release is None:
            if self.settings.release_manifest is not None or self.settings.release_expected_id:
                fail("Release configuration changed; restart the process")
            return
        try:
            if self.settings.release_expected_id != self.id:
                fail("Release pin changed; restart the process")
            if self.settings.release_manifest is None:
                fail("Release configuration removed; restart the process")
            if runtime_policy(self.settings) != self.release.runtime:
                fail()
            if bindings(self.settings) != self.release.bindings:
                fail()
        except (OSError, ValueError, TypeError, RecursionError):
            fail("Release configuration inputs are unavailable or invalid")

    @property
    def task_queue(self):
        # A release-specific queue prevents mixed binaries from consuming the same task.
        if self.release is None:
            return self.settings.task_queue
        return f"{self.release.runtime.task_queue}-{self.id[7:]}"
