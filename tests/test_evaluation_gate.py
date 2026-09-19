import copy
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_py.cli import app
from agent_py.domain import digest
from agent_py.evaluation import dataset
from agent_py.evaluation_gate import evaluate_gate, run_gate
from agent_py.retrieval_evaluation import RetrievalFixture, evaluate_retrieval


@pytest.fixture
def evidence(tmp_path):
    fixture = json.loads(Path("examples/retrieval-development.json").read_text())
    retrieval = evaluate_retrieval(Path("examples/retrieval-development.json"), tmp_path / "r.json")
    cases = [c for c in dataset() if c["split"] == "development"]
    simulation = dict(
        suite="simulation-conformance-v2",
        dataset_digest=digest(cases),
        not_a_model_benchmark=True,
        dataset_limitation="Synthetic gate test, not actual execution",
        split="development",
        passed=len(cases),
        total=len(cases),
        results=[
            dict(
                id=c["id"],
                family=c["family"],
                task_id=c["id"],
                expected=c["expected"],
                actual=c["expected"],
                passed=True,
                confirmed_operations=1,
                remote_effects=1,
                maximum_attempts=1,
                simulation=True,
            )
            for c in cases
        ],
    )
    policy = json.loads(Path("examples/evaluation-gate-policy.json").read_text())
    return [policy, simulation, copy.deepcopy(retrieval), copy.deepcopy(retrieval), fixture]


def status(values):
    result = evaluate_gate(*values)
    assert result["digest"] == digest(result["body"])
    assert result["body"]["production_ready"] is False
    return result["body"]["status"]


def test_gate_pass_is_reproducible_and_paired(evidence):
    first = evaluate_gate(*evidence)
    assert status(evidence) == "PASS"
    assert first == evaluate_gate(*evidence)
    assert len(first["body"]["paired_queries"]) == 12
    assert set(first["body"]["input_digests"]) == {
        "policy",
        "simulation",
        "baseline",
        "candidate",
        "fixture",
    }
    assert first["body"]["metrics"]["candidate"]["mean_recall"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "dataset",
        "fixture",
        "split",
        "missing_case",
        "duplicate_case",
        "fake_pass",
        "fake_summary",
        "reused_task",
        "query_missing",
        "query_duplicate",
        "unknown_document",
        "fake_metric",
        "fake_mean",
        "budget",
        "nonfinite",
        "bool_count",
        "missing_strategy",
        "duplicate_strategy",
        "too_few",
        "old_suite",
        "duplicate_document",
        "none_with_documents",
    ],
)
def test_invalid_or_incomplete_evidence_never_passes(evidence, mutation):
    policy, sim, base, candidate, fixture = evidence
    selected = candidate["results"][2]
    row = selected["queries"][0]
    if mutation == "dataset":
        candidate["dataset_digest"] = "0" * 64
    elif mutation == "fixture":
        fixture["documents"][0]["body"] += "changed"
    elif mutation == "split":
        sim["split"] = "holdout"
    elif mutation == "missing_case":
        sim["results"].pop()
    elif mutation == "duplicate_case":
        sim["results"][1] = sim["results"][0]
    elif mutation == "fake_pass":
        sim["results"][0]["remote_effects"] = 3
    elif mutation == "fake_summary":
        sim["passed"] = 0
    elif mutation == "reused_task":
        sim["results"][1]["task_id"] = sim["results"][0]["task_id"]
    elif mutation == "query_missing":
        selected["queries"].pop()
    elif mutation == "query_duplicate":
        selected["queries"][1] = row
    elif mutation == "unknown_document":
        row["retrieved"] = ["nonexistent"]
    elif mutation == "fake_metric":
        row["recall"] = 0.0
    elif mutation == "fake_mean":
        selected["mean_recall"] = 0.0
    elif mutation == "budget":
        row["budget"] += 1
    elif mutation == "nonfinite":
        row["recall"] = float("nan")
    elif mutation == "bool_count":
        row["chunks"] = True
    elif mutation == "missing_strategy":
        candidate["results"].pop()
    elif mutation == "duplicate_strategy":
        candidate["results"][0] = selected
    elif mutation == "too_few":
        policy["minimum_queries"] = 13
    elif mutation == "old_suite":
        sim["suite"] = "simulation-conformance-v1"
    elif mutation == "duplicate_document":
        row["retrieved"] += row["retrieved"]
    elif mutation == "none_with_documents":
        selected["strategy"] = "none"
        candidate["results"] = [selected]
        policy["candidate_strategy"] = "none"
    assert status(evidence) == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize("failure", ["side_effect", "redispatch", "outcome", "context", "quality"])
def test_valid_evidence_can_fail_hard_policy(evidence, failure):
    policy, sim, _, _, _ = evidence
    if failure in {"side_effect", "redispatch", "outcome"}:
        row = sim["results"][0]
        if failure == "side_effect":
            row["remote_effects"] += 1
        elif failure == "redispatch":
            row["maximum_attempts"] = 2
        else:
            row["actual"] = None
        row["passed"] = False
        sim["passed"] -= 1
    elif failure == "context":
        policy["maximum_mean_context_bytes"] = 0
    else:
        policy["candidate_strategy"] = "none"
    assert status(evidence) == "FAIL"


def test_per_query_regressions_block_even_when_average_is_unchanged(evidence):
    policy, _, base, candidate, fixture = evidence
    # Build two equally weighted queries with opposite winners, keeping aggregate quality equal.
    fixture["queries"] = fixture["queries"][:2]
    policy["minimum_queries"] = 2
    policy["minimum_recall"] = policy["minimum_mrr"] = 0.5
    fixture_digest = digest(RetrievalFixture.model_validate(fixture).model_dump())
    policy["retrieval_dataset_digest"] = fixture_digest
    for report, strategy, winner in ((base, "lexical", 0), (candidate, "bm25_rrf", 1)):
        report["dataset_digest"] = fixture_digest
        rows = []
        for i, query in enumerate(fixture["queries"]):
            retrieved = query["relevant"] if i == winner else []
            rows.append(
                dict(
                    id=query["id"],
                    retrieved=retrieved,
                    recall=float(bool(retrieved)),
                    precision=float(bool(retrieved)),
                    reciprocal_rank=float(bool(retrieved)),
                    context_bytes=100 if retrieved else 0,
                    budget=query.get("budget", 6000),
                    chunks=len(retrieved),
                )
            )
        report["results"] = [
            dict(strategy=strategy, queries=rows, mean_recall=0.5, mean_precision=0.5, mrr=0.5)
        ]
    result = evaluate_gate(*evidence)["body"]
    assert result["status"] == "FAIL"
    assert [c["name"] for c in result["checks"] if not c["passed"]] == ["regressed_queries"]


def write_inputs(evidence, tmp_path):
    paths = [tmp_path / f"input-{i}.json" for i in range(5)]
    for path, value in zip(paths, evidence):
        path.write_text(json.dumps(value))
    return paths


def test_cli_offline_exit_codes_and_missing_input_replaces_stale_pass(
    evidence, tmp_path, monkeypatch
):
    monkeypatch.setattr("agent_py.cli.build_service", lambda *_: pytest.fail("Must stay offline"))
    paths = write_inputs(evidence, tmp_path)
    output = tmp_path / "gate.json"
    args = ["evaluation-gate", *map(str, paths), "--output", str(output)]
    runner = CliRunner()
    assert runner.invoke(app, args).exit_code == 0
    evidence[0]["maximum_mean_context_bytes"] = 0
    paths[0].write_text(json.dumps(evidence[0]))
    assert runner.invoke(app, args).exit_code == 1
    paths[1].unlink()
    assert runner.invoke(app, args).exit_code == 2
    assert json.loads(output.read_text())["body"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert output.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', "[]", "x" * 2_000_001])
def test_strict_json_and_bounded_reads(evidence, tmp_path, raw):
    paths = write_inputs(evidence, tmp_path)
    paths[0].write_text(raw)
    result = run_gate(*paths, tmp_path / "gate.json")
    assert result["body"]["status"] == "INSUFFICIENT_EVIDENCE"


def test_cannot_overwrite_inputs_even_through_symlink(evidence, tmp_path):
    paths = write_inputs(evidence, tmp_path)
    alias = tmp_path / "alias.json"
    alias.symlink_to(paths[0])
    before = paths[0].read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        run_gate(*paths, alias)
    assert paths[0].read_bytes() == before
