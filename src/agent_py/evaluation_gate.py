"""Offline, fail-closed gates over existing simulation and retrieval evidence.

A passing report is local conformance evidence, never deployment authorization.
"""

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from agent_py.audit_signing import load_json
from agent_py.domain import Contract, digest
from agent_py.evaluation import dataset
from agent_py.retrieval_evaluation import RetrievalFixture

Ratio = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Count = Annotated[int, Field(ge=0, le=1_000_000)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Strategy = Literal["none", "lexical", "bm25_rrf"]


class GatePolicy(Contract):
    schema_version: Literal["evaluation-gate-policy-v1"] = Field(alias="schema")
    simulation_split: Literal["development", "calibration", "holdout"]
    simulation_dataset_digest: Hash
    retrieval_dataset_digest: Hash
    baseline_strategy: Strategy
    candidate_strategy: Strategy
    minimum_queries: int = Field(ge=1, le=1000)
    minimum_recall: Ratio
    minimum_mrr: Ratio
    maximum_mean_recall_drop: Ratio
    maximum_mean_mrr_drop: Ratio
    maximum_regressed_queries: int = Field(ge=0, le=1000)
    maximum_mean_context_bytes: int = Field(ge=0, le=100_000)


class ConformanceRow(Contract):
    id: str
    family: str
    task_id: str
    expected: str
    actual: str | None
    passed: bool
    confirmed_operations: Count
    remote_effects: Count
    maximum_attempts: Count
    simulation: Literal[True]


class ConformanceReport(Contract):
    suite: Literal["simulation-conformance-v2"]
    dataset_digest: Hash
    not_a_model_benchmark: Literal[True]
    dataset_limitation: str
    split: str
    passed: Count
    total: int = Field(ge=1, le=1000)
    results: list[ConformanceRow] = Field(min_length=1, max_length=1000)


class RetrievalRow(Contract):
    id: str
    retrieved: list[str] = Field(max_length=1000)
    recall: Ratio
    precision: Ratio
    reciprocal_rank: Ratio
    context_bytes: int = Field(ge=0, le=100_000)
    budget: int = Field(ge=1, le=100_000)
    chunks: Count


class StrategyReport(Contract):
    strategy: Strategy
    queries: list[RetrievalRow] = Field(min_length=1, max_length=1000)
    mean_recall: Ratio
    mean_precision: Ratio
    mrr: Ratio


class RetrievalReport(Contract):
    suite: Literal["retrieval-ablation-v1"]
    dataset: str
    dataset_digest: Hash
    not_a_model_benchmark: Literal[True]
    limitation: str
    results: list[StrategyReport] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique(self):
        if len({r.strategy for r in self.results}) != len(self.results):
            raise ValueError("Duplicate retrieval strategy")
        return self


class EvidenceError(ValueError):
    """Controlled diagnostics that never interpolate input values or business text."""


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def close(actual, expected):
    return math.isclose(actual, expected, rel_tol=0, abs_tol=1e-12)


def conformance(report, policy):
    selected = [case for case in dataset() if case["split"] == policy.simulation_split]
    require(
        report.dataset_digest == policy.simulation_dataset_digest == digest(selected),
        "Simulation dataset identity mismatch",
    )
    require(report.split == policy.simulation_split, "Simulation split mismatch")
    rows = {row.id: row for row in report.results}
    require(
        len(rows) == len(report.results) and set(rows) == {c["id"] for c in selected},
        "Simulation coverage missing, duplicated or unexpected",
    )
    require(len({r.task_id for r in report.results}) == len(rows), "Simulation task IDs reused")
    failures = []
    for case in selected:
        row = rows[case["id"]]
        require(
            row.family == case["family"] and row.expected == case["expected"],
            "Simulation case contract mismatch",
        )
        passed = (
            row.actual == case["expected"]
            and row.confirmed_operations == row.remote_effects
            and row.maximum_attempts <= 1
        )
        require(row.passed == passed, "Simulation claimed outcome contradicts evidence")
        if not passed:
            failures.append(row.id)
    require(
        report.total == len(rows) and report.passed == len(rows) - len(failures),
        "Simulation summary contradicts rows",
    )
    return failures


def retrieval(report, fixture, strategy, expected_digest):
    require(
        report.dataset_digest == expected_digest == digest(fixture.model_dump()),
        "Retrieval dataset identity mismatch",
    )
    require(report.dataset == fixture.name, "Retrieval dataset name mismatch")
    candidates = [r for r in report.results if r.strategy == strategy]
    require(len(candidates) == 1, "Required retrieval strategy missing")
    selected = candidates[0]
    rows = {row.id: row for row in selected.queries}
    require(
        len(rows) == len(selected.queries) and set(rows) == {q.id for q in fixture.queries},
        "Retrieval coverage missing, duplicated or unexpected",
    )
    documents = {d.id for d in fixture.documents}
    for query in fixture.queries:
        row = rows[query.id]
        require(
            row.budget == query.budget and row.context_bytes <= row.budget,
            "Retrieval budget mismatch or exceeded",
        )
        require(
            len(row.retrieved) == len(set(row.retrieved)) and set(row.retrieved) <= documents,
            "Invalid retrieved document identities",
        )
        require(row.chunks >= len(row.retrieved), "Retrieval chunk count contradicts documents")
        require(
            bool(row.chunks) == bool(row.retrieved) == bool(row.context_bytes),
            "Retrieval context accounting contradicts documents",
        )
        if strategy == "none":
            require(not row.retrieved, "No-retrieval baseline contains documents")
        relevant = set(query.relevant)
        hits = len(relevant & set(row.retrieved))
        recall = hits / len(relevant)
        precision = hits / len(row.retrieved) if row.retrieved else 0
        rr = next((1 / rank for rank, id in enumerate(row.retrieved, 1) if id in relevant), 0)
        require(
            close(row.recall, recall)
            and close(row.precision, precision)
            and close(row.reciprocal_rank, rr),
            "Retrieval metrics contradict relevance labels",
        )
        # Gate thresholds use recomputed values, not the accepted rounding in input summaries.
        row.recall, row.precision, row.reciprocal_rank = recall, precision, rr
    means = {
        "mean_recall": sum(r.recall for r in rows.values()) / len(rows),
        "mean_precision": sum(r.precision for r in rows.values()) / len(rows),
        "mrr": sum(r.reciprocal_rank for r in rows.values()) / len(rows),
        "mean_context_bytes": sum(r.context_bytes for r in rows.values()) / len(rows),
    }
    require(
        all(
            close(getattr(selected, key), means[key])
            for key in ("mean_recall", "mean_precision", "mrr")
        ),
        "Retrieval summary contradicts rows",
    )
    return rows, means


def evaluate_gate(policy_data, simulation_data, baseline_data, candidate_data, fixture_data):
    """Deterministic gate: malformed/incomplete evidence never becomes a passing decision."""
    report = {
        "schema": "evaluation-gate-v1",
        "status": "INSUFFICIENT_EVIDENCE",
        "production_ready": False,
        "not_a_model_benchmark": True,
        "checks": [],
        "paired_queries": [],
        "limitations": [
            "Self-reported local evidence; input digests do not authenticate execution or code version.",
            "Repeated simulation templates and authored retrieval labels do not measure model quality.",
            "Context bytes are not inference cost, token usage or latency; no statistical significance claim.",
        ],
    }
    try:
        inputs = dict(
            policy=policy_data,
            simulation=simulation_data,
            baseline=baseline_data,
            candidate=candidate_data,
            fixture=fixture_data,
        )
        report["input_digests"] = {name: digest(value) for name, value in inputs.items()}
        policy = GatePolicy.model_validate(policy_data)
        simulation = ConformanceReport.model_validate(simulation_data)
        fixture = RetrievalFixture.model_validate(fixture_data)
        baseline = RetrievalReport.model_validate(baseline_data)
        candidate = RetrievalReport.model_validate(candidate_data)
        failures = conformance(simulation, policy)
        base_rows, base = retrieval(
            baseline, fixture, policy.baseline_strategy, policy.retrieval_dataset_digest
        )
        candidate_rows, current = retrieval(
            candidate, fixture, policy.candidate_strategy, policy.retrieval_dataset_digest
        )
        require(len(fixture.queries) >= policy.minimum_queries, "Insufficient query sample count")
        report["strategies"] = {
            "baseline": policy.baseline_strategy,
            "candidate": policy.candidate_strategy,
        }
        report["metrics"] = {"baseline": base, "candidate": current}
        regressed = 0
        for id in sorted(base_rows):
            before, after = base_rows[id], candidate_rows[id]
            recall_delta = after.recall - before.recall
            mrr_delta = after.reciprocal_rank - before.reciprocal_rank
            worse = recall_delta < -1e-12 or mrr_delta < -1e-12
            regressed += worse
            report["paired_queries"].append(
                dict(
                    id=id,
                    recall_delta=recall_delta,
                    reciprocal_rank_delta=mrr_delta,
                    context_bytes_delta=after.context_bytes - before.context_bytes,
                    regressed=worse,
                )
            )
        checks = [
            ("simulation_conformance", not failures, len(failures), 0),
            (
                "minimum_recall",
                current["mean_recall"] >= policy.minimum_recall,
                current["mean_recall"],
                policy.minimum_recall,
            ),
            (
                "minimum_mrr",
                current["mrr"] >= policy.minimum_mrr,
                current["mrr"],
                policy.minimum_mrr,
            ),
            (
                "mean_recall_drop",
                base["mean_recall"] - current["mean_recall"]
                <= policy.maximum_mean_recall_drop + 1e-12,
                base["mean_recall"] - current["mean_recall"],
                policy.maximum_mean_recall_drop,
            ),
            (
                "mean_mrr_drop",
                base["mrr"] - current["mrr"] <= policy.maximum_mean_mrr_drop + 1e-12,
                base["mrr"] - current["mrr"],
                policy.maximum_mean_mrr_drop,
            ),
            (
                "regressed_queries",
                regressed <= policy.maximum_regressed_queries,
                regressed,
                policy.maximum_regressed_queries,
            ),
            (
                "mean_context_bytes",
                current["mean_context_bytes"] <= policy.maximum_mean_context_bytes,
                current["mean_context_bytes"],
                policy.maximum_mean_context_bytes,
            ),
        ]
        report["checks"] = [
            dict(name=n, passed=ok, observed=v, threshold=t) for n, ok, v, t in checks
        ]
        report["failed_simulation_cases"] = failures
        report["status"] = "PASS" if all(ok for _, ok, _, _ in checks) else "FAIL"
    except EvidenceError as exc:
        report["reason"] = str(exc)
    except (ValueError, TypeError, KeyError, RecursionError):
        # Validation errors may contain fixture text. Keep diagnostics bounded and non-sensitive.
        report["reason"] = (
            "Evidence or policy invalid, inconsistent, incomplete, or dataset identity mismatched"
        )
    return {"body": report, "digest": digest(report)}


def run_gate(
    policy: Path, simulation: Path, baseline: Path, candidate: Path, fixture: Path, output: Path
):
    paths = (policy, simulation, baseline, candidate, fixture)
    if output.resolve() in {p.resolve() for p in paths}:
        raise ValueError("Gate output must not overwrite an input")
    # Bounded strict JSON loading; no URL discovery, Service, credentials or DB access.
    try:
        values = [
            load_json(path, 2_000_000 if i in (0, 4) else 10_000_000)
            for i, path in enumerate(paths)
        ]
        report = evaluate_gate(*values)
    except (OSError, ValueError, RecursionError):
        report = evaluate_gate(None, None, None, None, None)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=output.parent, prefix=".gate-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return report
