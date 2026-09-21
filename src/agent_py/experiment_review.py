"""Read-only archive verification and explicitly reviewed development-case curation."""

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from agent_py.domain import Contract, digest
from agent_py.experiment_datasets import (
    DatasetSnapshot,
    ExperimentDataset,
    Identifier,
    publish_json,
    read_snapshot,
)
from agent_py.jsonio import load_json
from agent_py.retrieval_experiments import ExperimentPlan, jobs, read_record, scores, summarize

SHA = r"^[a-f0-9]{64}$"


class ArchivedImplementation(Contract):
    sources: dict[str, str] = Field(min_length=1, max_length=5000)
    uv_lock: str = Field(alias="uv.lock", pattern=SHA)
    python: str = Field(min_length=1, max_length=80)
    packages: dict[str, str]

    @model_validator(mode="after")
    def bounded_identity(self):
        import re

        if set(self.packages) != {"pydantic", "sqlalchemy"} or any(
            not value or len(value) > 80 for value in self.packages.values()
        ):
            raise ValueError("Invalid dependency identity")
        for path, value in self.sources.items():
            if (
                len(path) > 300
                or path.startswith("/")
                or "\\" in path
                or any(p in {"", ".", ".."} for p in path.split("/"))
                or not path.endswith(".py")
                or not re.fullmatch(SHA, value)
            ):
                raise ValueError("Invalid source identity")
        return self


class ArchivedPlan(Contract):
    plan: ExperimentPlan
    implementation: ArchivedImplementation

    @model_validator(mode="after")
    def identity_binding(self):
        if digest(self.implementation.model_dump(by_alias=True)) != self.plan.implementation_digest:
            raise ValueError("Archived implementation digest mismatch")
        return self


def regular_file(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Evidence must be a regular file")
    return path


@contextmanager
def read_archive(directory):
    """Cooperate with the runner without creating a lock or modifying archived files."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Archive must be a real directory")
    fd = os.open(regular_file(directory / ".lock"), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Experiment is still being written") from None
        yield
    finally:
        os.close(fd)


def _inspect(directory):
    snapshot = read_snapshot(regular_file(directory / "dataset.json"))
    raw_plan = load_json(regular_file(directory / "plan.json"), 2_000_000)
    archived = ArchivedPlan.model_validate(raw_plan)
    if digest(raw_plan) != digest(archived.model_dump(by_alias=True)):
        raise ValueError("Archive plan is not canonical")
    plan = archived.plan
    if snapshot.content_digest != plan.dataset_digest:
        raise ValueError("Plan dataset binding mismatch")
    cases = [case for case in snapshot.dataset.cases if case.split == plan.split]
    if not cases or len(cases) * plan.repeats * 3 > 3000:
        raise ValueError("Invalid archive schedule")
    directory_records = directory / "records"
    if directory_records.is_symlink() or not directory_records.is_dir():
        raise ValueError("Invalid record directory")
    schedule = list(jobs(plan, cases))
    expected = {key + ".json" for _, _, _, key in schedule}
    if any(p.name not in expected for p in directory_records.glob("*.json")):
        raise ValueError("Unexpected archive record")
    records, manifest, failures = [], {}, {}
    for case, strategy, repeat, key in schedule:
        path = directory_records / (key + ".json")
        if not path.exists() and not path.is_symlink():
            manifest[key] = None
            continue
        record = read_record(regular_file(path), case, strategy, repeat, key)
        records.append(record)
        manifest[key] = digest(record.model_dump())
        # The intentionally empty baseline is not a source of improvement cases.
        if strategy != "none" and (record.status == "failed" or scores(record, case)["recall"] < 1):
            failures.setdefault(case.id, []).append(
                {
                    "job_digest": key,
                    "strategy": strategy,
                    "repeat": repeat,
                    "reason": "EXECUTION_FAILED"
                    if record.status == "failed"
                    else "RECALL_SHORTFALL",
                }
            )
    summary = summarize(plan, cases, records)
    report_path = directory / "report.json"
    report_status = "MISSING"
    if report_path.exists() or report_path.is_symlink():
        try:
            stored = load_json(regular_file(report_path), 4_000_000)
            report_status = "MATCH" if digest(stored) == digest(summary) else "MISMATCH"
        except (ValueError, OSError):
            report_status = "UNREADABLE"
    evidence_digest = digest(
        {"dataset": snapshot.content_digest, "plan": digest(raw_plan), "records": manifest}
    )
    result = {
        "format": "agent-experiment-audit/v1",
        "evidence_digest": evidence_digest,
        "implementation_digest": plan.implementation_digest,
        "evidence_status": "VALID",
        "report_status": report_status,
        "audit_status": (
            "REPORT_MISMATCH" if report_status != "MATCH" else summary["execution_status"]
        ),
        "recomputed_report": summary,
        "review_queue": [
            {
                "case_id": case_id,
                "eligible_for_development": plan.split == "development",
                "failures": items,
            }
            for case_id, items in sorted(failures.items())
        ],
        "limitations": [
            "Hashes verify internal consistency, not signed provenance or actual execution",
            "Context bodies are not stored; context digests cannot be independently reconstructed",
            "Current retrieval code is never executed; metrics use the current v1 audit implementation",
            "Filesystem locking protects cooperative writers on local POSIX filesystems only",
        ],
    }
    return snapshot, result


def audit_experiment(directory: Path, *, expected_evidence_digest: str | None = None):
    with read_archive(directory):
        result = _inspect(directory)[1]
        if (
            expected_evidence_digest is not None
            and result["evidence_digest"] != expected_evidence_digest
        ):
            raise ValueError("Independent evidence pin mismatch")
        return result


class ReviewDecision(Contract):
    case_id: Identifier
    reviewer: Identifier
    rubric_version: Identifier
    reference_verified: Literal[True]
    redaction_verified: Literal[True]


class CurationRequest(Contract):
    format: Literal["agent-experiment-curation/v1"] = "agent-experiment-curation/v1"
    evidence_digest: str = Field(pattern=SHA)
    dataset_name: Identifier
    decisions: list[ReviewDecision] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_decisions(self):
        if len({d.case_id for d in self.decisions}) != len(self.decisions):
            raise ValueError("Duplicate review decision")
        return self


def curate_experiment(directory: Path, request_path: Path, output: Path):
    if output.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Curation output must be outside the source archive")
    raw = load_json(request_path, 500_000)
    request = CurationRequest.model_validate(raw)
    # Require literal JSON booleans, not numeric equivalents accepted by Literal[True].
    if digest(raw) != digest(request.model_dump()):
        raise ValueError("Curation request is not canonical")
    with read_archive(directory):
        snapshot, audit = _inspect(directory)
        if (
            request.evidence_digest != audit["evidence_digest"]
            or audit["report_status"] != "MATCH"
            or audit["recomputed_report"]["split"] != "development"
        ):
            raise ValueError("Curation requires matching, audited development evidence")
        eligible = {r["case_id"]: r for r in audit["review_queue"]}
        selected = {d.case_id for d in request.decisions}
        if not selected <= eligible.keys():
            raise ValueError("Only observed non-baseline failures may be curated")
        # Preserve inputs, labels, entity and family IDs; no renaming or holdout copying.
        dataset = ExperimentDataset(
            name=request.dataset_name,
            provenance=snapshot.dataset.provenance,
            cases=[case for case in snapshot.dataset.cases if case.id in selected],
        )
        frozen = DatasetSnapshot(content_digest=digest(dataset.model_dump()), dataset=dataset)
        lineage = {
            "format": "agent-experiment-curation-lineage/v1",
            "source_evidence_digest": audit["evidence_digest"],
            "source_dataset_digest": snapshot.content_digest,
            "source_implementation_digest": audit["implementation_digest"],
            "output_dataset_digest": frozen.content_digest,
            "review": request.model_dump(),
            "review_digest": digest(request.model_dump()),
            "failures": [eligible[key] for key in sorted(selected)],
            "review_identity": "OPERATOR_DECLARED_NOT_AUTHENTICATED",
            "release_decision": "NOT_ASSESSED",
        }
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        publish_json(output / "dataset.json", frozen.model_dump(), max_bytes=4_000_000)
        publish_json(output / "lineage.json", lineage)
        # Written last: interrupted publication is not a completed curation bundle.
        publish_json(
            output / "complete.json",
            {"dataset_digest": frozen.content_digest, "lineage_digest": digest(lineage)},
        )
        return lineage
