"""Offline paired retrieval experiments with immutable inputs and resumable evidence."""

import fcntl
import math
import os
import platform
import random
import time
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import Field, model_validator

from agent_py.context import ContextCompiler
from agent_py.db import Database, Document, Grant
from agent_py.domain import Contract, Principal, digest
from agent_py.experiment_datasets import (
    Identifier,
    Split,
    Strategy,
    file_digest,
    publish_json,
    read_snapshot,
)
from agent_py.jsonio import load_json

STRATEGIES = ("none", "lexical", "bm25_rrf")


class ExperimentPlan(Contract):
    format: Literal["agent-retrieval-experiment/v1"] = "agent-retrieval-experiment/v1"
    dataset_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    implementation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    split: Split
    repeats: int = Field(ge=1, le=10)
    strategies: list[Strategy] = Field(default_factory=lambda: list(STRATEGIES))

    @model_validator(mode="after")
    def fixed_comparison(self):
        if self.strategies != list(STRATEGIES):
            raise ValueError("This runner requires all three fixed strategies")
        return self


class JobRecord(Contract):
    schema_version: Literal[1] = 1
    job_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_id: Identifier
    strategy: Strategy
    repeat: int = Field(ge=0, le=9)
    status: Literal["completed", "failed"]
    retrieved: list[str] = Field(default_factory=list, max_length=100)
    context_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    context_bytes: int = Field(default=0, ge=0, le=100_000)
    chunks: int = Field(default=0, ge=0, le=100_000)
    elapsed_ns: int = Field(ge=1)
    error: Literal["EXECUTION_FAILED"] | None = None

    @model_validator(mode="after")
    def status_fields(self):
        if len(set(self.retrieved)) != len(self.retrieved):
            raise ValueError("Retrieved document IDs must be unique")
        if self.status == "completed":
            if self.error is not None or self.context_digest is None:
                raise ValueError("Completed result needs context identity and no error")
        elif (
            self.error is None
            or self.context_digest is not None
            or self.retrieved
            or self.context_bytes
            or self.chunks
        ):
            raise ValueError("Failed result cannot pretend to contain complete retrieval evidence")
        return self


def implementation_identity():
    root = Path(__file__).resolve().parent
    files = {str(p.relative_to(root)): file_digest(p) for p in sorted(root.rglob("*.py"))}
    lock = root.parents[1] / "uv.lock"
    if not lock.is_file():
        raise ValueError("Run from a checkout with its dependency lock")
    return {
        "sources": files,
        "uv.lock": file_digest(lock),
        "python": platform.python_version(),
        "packages": {p: version(p) for p in ("pydantic", "sqlalchemy")},
    }


@contextmanager
def exclusive_run(directory):
    descriptor = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Experiment is already running") from None
        yield
    finally:
        os.close(descriptor)


def jobs(plan, cases):
    for case in cases:
        for repeat in range(plan.repeats):
            # Rotate order without changing comparison identities or consulting results.
            order = STRATEGIES[repeat % 3 :] + STRATEGIES[: repeat % 3]
            for strategy in order:
                key = digest(
                    {
                        "plan": digest(plan.model_dump()),
                        "case": case.id,
                        "repeat": repeat,
                        "strategy": strategy,
                    }
                )
                yield case, strategy, repeat, key


def retrieve(case, strategy):
    """Only query/documents enter the compiler; reference labels stay outside the DB."""
    if strategy == "none":
        return dict(retrieved=[], context_digest=digest([]), context_bytes=0, chunks=0)
    principal = Principal(
        tenant_id="experiment",
        subject="reader",
        roles=[],
        projects=["fixture"],
        environments=["lab"],
    )
    with TemporaryDirectory(prefix="agent-experiment-") as directory:
        db = Database(f"sqlite:///{directory}/isolated.db")
        try:
            db.create_schema()
            with db.session(principal.tenant_id) as s:
                s.add(
                    Grant(
                        tenant_id=principal.tenant_id,
                        subject=principal.subject,
                        project="fixture",
                        environment="lab",
                    )
                )
                for document in case.documents:
                    s.add(
                        Document(
                            **document.model_dump(),
                            tenant_id=principal.tenant_id,
                            project="fixture",
                            version=digest(document.body),
                            allowed_subjects=[principal.subject],
                        )
                    )
            compiler = ContextCompiler(db, strategy)
            bundle = compiler.compile(
                principal, "fixture", "lab", case.query, budget=case.context_budget
            )
            compiler.validate(principal, "fixture", "lab", bundle)
            return dict(
                retrieved=list(dict.fromkeys(d["id"] for d in bundle.documents)),
                context_digest=bundle.digest,
                context_bytes=bundle.estimated_tokens,
                chunks=len(bundle.documents),
            )
        finally:
            db.engine.dispose()


def validate_record(record, case, strategy, repeat, key):
    if (record.case_id, record.strategy, record.repeat, record.job_digest) != (
        case.id,
        strategy,
        repeat,
        key,
    ):
        raise ValueError("Record identity does not match frozen plan")
    if (
        not set(record.retrieved) <= {d.id for d in case.documents}
        or record.context_bytes > case.context_budget
    ):
        raise ValueError("Record is outside frozen evidence or context budget")
    if record.status == "completed":
        if record.chunks < len(record.retrieved) or (bool(record.chunks) != bool(record.retrieved)):
            raise ValueError("Invalid retrieval cardinality")
        if strategy == "none" and (
            record.retrieved
            or record.context_bytes
            or record.chunks
            or record.context_digest != digest([])
        ):
            raise ValueError("No-retrieval baseline contains retrieval")


def read_record(path, case, strategy, repeat, key):
    envelope = load_json(path, 100_000)
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"record", "content_digest"}
        or digest(envelope["record"]) != envelope["content_digest"]
    ):
        raise ValueError("Record digest mismatch")
    record = JobRecord.model_validate(envelope["record"])
    validate_record(record, case, strategy, repeat, key)
    return record


def scores(record, case):
    relevant = set(case.relevant)
    hits = relevant & set(record.retrieved)
    return {
        "recall": len(hits) / len(relevant),
        "precision": len(hits) / len(record.retrieved) if record.retrieved else 0,
        "reciprocal_rank": next(
            (1 / n for n, doc in enumerate(record.retrieved, 1) if doc in relevant), 0
        ),
    }


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def paired(cases, indexed, repeats, strategy):
    groups, rows = {}, []
    for case in cases:
        deltas = []
        for repeat in range(repeats):
            baseline, candidate = (
                indexed.get((case.id, "lexical", repeat)),
                indexed.get((case.id, strategy, repeat)),
            )
            if (
                baseline is None
                or candidate is None
                or baseline.status != "completed"
                or candidate.status != "completed"
            ):
                continue
            deltas.append(scores(candidate, case)["recall"] - scores(baseline, case)["recall"])
        if len(deltas) != repeats:
            continue
        delta = sum(deltas) / repeats
        groups.setdefault(case.entity_group, []).append(delta)
        rows.append(
            {
                "case_id": case.id,
                "entity_group": case.entity_group,
                "template_family": case.template_family,
                "recall_delta": delta,
            }
        )
    interval = None
    if len(groups) >= 5 and len(rows) == len(cases):
        rng = random.Random(0)
        clusters = list(groups.values())
        samples = []
        for _ in range(1000):
            values = [
                value for cluster in rng.choices(clusters, k=len(clusters)) for value in cluster
            ]
            samples.append(sum(values) / len(values))
        interval = [percentile(samples, 0.025), percentile(samples, 0.975)]
    families = {}
    for family in sorted({r["template_family"] for r in rows}):
        values = [r["recall_delta"] for r in rows if r["template_family"] == family]
        families[family] = {"cases": len(values), "mean_recall_delta": sum(values) / len(values)}
    return {
        "baseline": "lexical",
        "candidate": strategy,
        "paired_cases": len(rows),
        "missing_cases": len(cases) - len(rows),
        "entity_groups": len(groups),
        "mean_recall_delta": sum(r["recall_delta"] for r in rows) / len(rows) if rows else None,
        "entity_cluster_bootstrap_95": interval,
        "bootstrap_seed": 0,
        "bootstrap_samples": 1000,
        "assessment": "EXPLORATORY",
        "cases": rows,
        "families": families,
    }


def summarize(plan, cases, records):
    indexed = {(r.case_id, r.strategy, r.repeat): r for r in records}
    expected = len(cases) * plan.repeats * len(STRATEGIES)
    completed = sum(r.status == "completed" for r in records)
    results = []
    for strategy in STRATEGIES:
        selected = [r for r in records if r.strategy == strategy and r.status == "completed"]
        by_id = {case.id: case for case in cases}
        quality = [scores(r, by_id[r.case_id]) for r in selected]
        times = [r.elapsed_ns / 1_000_000 for r in selected]
        results.append(
            {
                "strategy": strategy,
                "completed": len(selected),
                "missing_or_failed": len(cases) * plan.repeats - len(selected),
                "metrics": {
                    name: sum(s[name] for s in quality) / len(quality) if quality else None
                    for name in ("recall", "precision", "reciprocal_rank")
                },
                "job_p50_ms": percentile(times, 0.5),
                "job_p95_ms": percentile(times, 0.95),
            }
        )
    return {
        "format": "agent-retrieval-experiment-report/v1",
        "plan_digest": digest(plan.model_dump()),
        "dataset_digest": plan.dataset_digest,
        "split": plan.split,
        "execution_status": "COMPLETE" if completed == expected else "INCOMPLETE",
        "expected_jobs": expected,
        "completed_jobs": completed,
        "failed_jobs": sum(r.status == "failed" for r in records),
        "pending_jobs": expected - len(records),
        "quality_scope": "retrieval_only",
        "model_quality": "NOT_ASSESSED",
        "release_decision": "NOT_ASSESSED",
        "langfuse": "NOT_UPLOADED",
        "results": results,
        "paired": [paired(cases, indexed, plan.repeats, s) for s in ("none", "bm25_rrf")],
        "limitations": [
            "Authored relevance labels are not independently verified",
            "Group declarations and exact input checks do not prove semantic non-leakage",
            "Repeats are not independent cases; intervals resample entity groups",
            "Job latency includes disposable DB setup and teardown, not model latency",
            "No model, judge, sandbox or enterprise action is executed",
        ],
    }


def run_experiment(
    snapshot_path: Path, output: Path, *, split="development", repeats=1, resume=False
):
    snapshot = read_snapshot(snapshot_path)
    identity = implementation_identity()
    plan = ExperimentPlan(
        dataset_digest=snapshot.content_digest,
        implementation_digest=digest(identity),
        split=split,
        repeats=repeats,
    )
    cases = [case for case in snapshot.dataset.cases if case.split == plan.split]
    if not cases or len(cases) * repeats * 3 > 3000:
        raise ValueError("Selected split must contain 1..3000 jobs")
    if resume:
        if not output.is_dir() or output.is_symlink():
            raise ValueError("Resume requires an existing real run directory")
    else:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with exclusive_run(output):
        if resume:
            if digest(load_json(output / "plan.json")) != digest(
                {
                    "plan": plan.model_dump(),
                    "implementation": identity,
                }
            ):
                raise ValueError(
                    "Frozen plan, dataset, implementation or dependency versions changed"
                )
            if read_snapshot(output / "dataset.json").model_dump() != snapshot.model_dump():
                raise ValueError("Local dataset copy changed")
        else:
            publish_json(output / "dataset.json", snapshot.model_dump(), max_bytes=4_000_000)
            publish_json(
                output / "plan.json", {"plan": plan.model_dump(), "implementation": identity}
            )
            (output / "records").mkdir(mode=0o700)
        if not (output / "records").is_dir() or (output / "records").is_symlink():
            raise ValueError("Result directory must be a real directory")
        schedule = list(jobs(plan, cases))
        expected_paths = {key + ".json" for _, _, _, key in schedule}
        if any(path.name not in expected_paths for path in (output / "records").glob("*.json")):
            raise ValueError("Unexpected result file")
        records, pending = [], []
        # Validate ALL old evidence before starting any new job.
        for case, strategy, repeat, key in schedule:
            path = output / "records" / (key + ".json")
            if path.exists():
                records.append(read_record(path, case, strategy, repeat, key))
            else:
                pending.append((case, strategy, repeat, key))
        publish_json(output / "report.json", summarize(plan, cases, records), replace=True)
        for case, strategy, repeat, key in pending:
            started = time.perf_counter_ns()
            try:
                result = retrieve(case, strategy)
                record = JobRecord(
                    job_digest=key,
                    case_id=case.id,
                    strategy=strategy,
                    repeat=repeat,
                    status="completed",
                    elapsed_ns=max(1, time.perf_counter_ns() - started),
                    **result,
                )
                validate_record(record, case, strategy, repeat, key)
            except Exception:
                record = JobRecord(
                    job_digest=key,
                    case_id=case.id,
                    strategy=strategy,
                    repeat=repeat,
                    status="failed",
                    error="EXECUTION_FAILED",
                    elapsed_ns=max(1, time.perf_counter_ns() - started),
                )
            publish_json(
                output / "records" / (key + ".json"),
                {"record": record.model_dump(), "content_digest": digest(record.model_dump())},
            )
            records.append(record)
        report = summarize(plan, cases, records)
        if implementation_identity() != identity:
            report["execution_status"] = "INVALID"
            report["error"] = "IMPLEMENTATION_CHANGED"
        publish_json(output / "report.json", report, replace=True)
        return report
