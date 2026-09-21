"""Frozen inputs, actual retrieval, failure retention and interrupt/resume contracts."""

import json
import stat
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from agent_py import retrieval_experiments as runner
from agent_py.cli import app
from agent_py.domain import digest
from agent_py.experiment_datasets import ExperimentDataset, freeze_dataset, read_snapshot
from agent_py.jsonio import load_json

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/retrieval-experiment.json"


@pytest.fixture
def frozen(tmp_path):
    path = tmp_path / "dataset.json"
    freeze_dataset(EXAMPLE, path)
    return path


def test_freeze_private_digest_and_no_overwrite(frozen):
    snapshot = read_snapshot(frozen)
    assert snapshot.content_digest == digest(snapshot.dataset.model_dump())
    assert stat.S_IMODE(frozen.stat().st_mode) == 0o600
    before = frozen.read_bytes()
    with pytest.raises(FileExistsError):
        freeze_dataset(EXAMPLE, frozen)
    assert frozen.read_bytes() == before


@pytest.mark.parametrize(
    "fault", ["entity", "family", "duplicate_input", "unknown_reference", "duplicate_id"]
)
def test_dataset_rejects_split_leakage_and_invalid_references(fault):
    raw = load_json(EXAMPLE)
    a, b = raw["cases"][0], raw["cases"][2]
    if fault == "entity":
        b["entity_group"] = a["entity_group"]
    elif fault == "family":
        b["template_family"] = a["template_family"]
    elif fault == "duplicate_input":
        b["query"] = "  " + a["query"].upper() + "  "
        b["documents"] = deepcopy(a["documents"])
        b["documents"].reverse()
        b["documents"][0]["source"] = "fixture://different-location"
    elif fault == "unknown_reference":
        b["relevant"] = ["missing"]
    else:
        b["id"] = a["id"]
    with pytest.raises(ValueError):
        ExperimentDataset.model_validate(raw)


def test_snapshot_tampering_and_duplicate_json_fields_are_rejected(frozen, tmp_path):
    raw = load_json(frozen)
    raw["dataset"]["cases"][0]["relevant"] = ["distractor"]
    frozen.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="digest"):
        read_snapshot(frozen)
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match="Duplicate"):
        freeze_dataset(bad, tmp_path / "output.json")


def test_actual_comparison_scores_from_ids_and_resume_never_repeats(frozen, tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("No HTTP client may be constructed")

    monkeypatch.setattr(httpx, "Client", no_network)
    output = tmp_path / "run"
    report = runner.run_experiment(frozen, output, repeats=2)
    assert report["execution_status"] == "COMPLETE" and report["expected_jobs"] == 12
    assert report["model_quality"] == report["release_decision"] == "NOT_ASSESSED"
    assert report["langfuse"] == "NOT_UPLOADED"
    assert [r["strategy"] for r in report["results"]] == ["none", "lexical", "bm25_rrf"]
    assert report["results"][0]["metrics"]["recall"] == 0
    assert all(r["metrics"]["recall"] == 1 for r in report["results"][1:])
    assert report["paired"][0]["paired_cases"] == 2
    assert report["paired"][0]["entity_groups"] == 2  # repeats are not independent cases
    assert report["paired"][0]["entity_cluster_bootstrap_95"] is None
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    records = {p.name: p.read_bytes() for p in (output / "records").glob("*.json")}
    assert len(records) == 12
    monkeypatch.setattr(runner, "retrieve", lambda *a: pytest.fail("Completed work replayed"))
    assert runner.run_experiment(frozen, output, repeats=2, resume=True) == report
    assert {p.name: p.read_bytes() for p in (output / "records").glob("*.json")} == records
    for path in output.rglob("*.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_interrupted_run_resumes_only_missing_jobs(frozen, tmp_path, monkeypatch):
    output = tmp_path / "run"
    original, calls = runner.retrieve, []

    def interrupt(case, strategy):
        calls.append((case.id, strategy))
        if len(calls) == 2:
            raise KeyboardInterrupt()
        return original(case, strategy)

    monkeypatch.setattr(runner, "retrieve", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run_experiment(frozen, output)
    paths = list((output / "records").glob("*.json"))
    assert len(paths) == 1
    before = paths[0].read_bytes()
    assert load_json(output / "report.json")["execution_status"] == "INCOMPLETE"
    calls.clear()
    monkeypatch.setattr(runner, "retrieve", lambda c, s: calls.append((c.id, s)) or original(c, s))
    assert runner.run_experiment(frozen, output, resume=True)["execution_status"] == "COMPLETE"
    assert len(calls) == 5 and paths[0].read_bytes() == before


def test_failures_are_missing_evidence_not_passes_or_silent_retries(frozen, tmp_path, monkeypatch):
    output = tmp_path / "run"
    original = runner.retrieve

    def fail(case, strategy):
        if case.id == "case-1" and strategy == "bm25_rrf":
            raise RuntimeError("PRIVATE_EXCEPTION")
        return original(case, strategy)

    monkeypatch.setattr(runner, "retrieve", fail)
    report = runner.run_experiment(frozen, output)
    assert report["execution_status"] == "INCOMPLETE"
    assert report["completed_jobs"] == 5 and report["failed_jobs"] == 1
    assert report["paired"][1]["missing_cases"] == 1
    assert report["paired"][1]["entity_cluster_bootstrap_95"] is None
    assert all("PRIVATE_EXCEPTION" not in path.read_text() for path in output.rglob("*.json"))
    monkeypatch.setattr(runner, "retrieve", lambda *a: pytest.fail("Failed job retried"))
    assert runner.run_experiment(frozen, output, resume=True) == report


@pytest.mark.parametrize("fault", ["digest", "unknown_document", "plan", "source", "other_job"])
def test_resume_rejects_changed_evidence_before_any_new_work(frozen, tmp_path, monkeypatch, fault):
    output = tmp_path / "run"
    runner.run_experiment(frozen, output)
    record_path = next((output / "records").glob("*.json"))
    if fault in {"digest", "unknown_document"}:
        raw = load_json(record_path)
        raw["record"]["retrieved"] = ["PRIVATE_UNKNOWN_DOCUMENT"]
        if fault == "unknown_document":
            raw["content_digest"] = digest(raw["record"])
        record_path.write_text(json.dumps(raw))
    elif fault == "plan":
        raw = load_json(output / "plan.json")
        raw["plan"]["repeats"] = 2
        (output / "plan.json").write_text(json.dumps(raw))
    elif fault == "source":
        monkeypatch.setattr(runner, "implementation_identity", lambda: {"different": True})
    else:
        (output / "records/unexpected.json").write_text("{}")
    monkeypatch.setattr(
        runner, "retrieve", lambda *a: pytest.fail("Corruption must precede execution")
    )
    with pytest.raises(ValueError):
        runner.run_experiment(frozen, output, resume=True)


def test_single_writer_lock_and_changed_configuration_are_rejected(frozen, tmp_path):
    output = tmp_path / "run"
    runner.run_experiment(frozen, output)
    with runner.exclusive_run(output):
        with pytest.raises(ValueError, match="already running"):
            runner.run_experiment(frozen, output, resume=True)
    with pytest.raises(ValueError):
        runner.run_experiment(frozen, output, repeats=2, resume=True)
    with pytest.raises(ValueError):
        runner.run_experiment(frozen, output, split="holdout", resume=True)
    with pytest.raises(FileExistsError):
        runner.run_experiment(frozen, output)


def test_source_changes_during_execution_invalidate_report(frozen, tmp_path, monkeypatch):
    identities = iter([{"version": "before"}, {"version": "after"}])
    monkeypatch.setattr(runner, "implementation_identity", lambda: next(identities))
    report = runner.run_experiment(frozen, tmp_path / "run")
    assert report["execution_status"] == "INVALID"
    assert report["error"] == "IMPLEMENTATION_CHANGED"


def test_cli_freeze_run_resume_and_invalid_input_do_not_use_app_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://unreachable/private")
    monkeypatch.setenv("AGENT_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("AGENT_MODEL_API_KEY", "PRIVATE_KEY")
    cli = CliRunner()
    snapshot, output = tmp_path / "dataset.json", tmp_path / "run"
    assert cli.invoke(app, ["experiment-freeze", str(EXAMPLE), str(snapshot)]).exit_code == 0
    result = cli.invoke(app, ["experiment-run", str(snapshot), str(output)])
    assert result.exit_code == 0, result.output
    assert "COMPLETE" in result.output
    assert (
        cli.invoke(app, ["experiment-run", str(snapshot), str(output), "--resume"]).exit_code == 0
    )
    invalid = cli.invoke(
        app,
        ["experiment-run", str(snapshot), str(output), "--split", "PRIVATE_INVALID", "--resume"],
    )
    assert invalid.exit_code != 0 and "PRIVATE_INVALID" not in invalid.output


def test_cluster_intervals_and_family_counts_use_cases_not_repeats(tmp_path):
    dataset = load_json(EXAMPLE)
    template = dataset["cases"][0]
    dataset["cases"] = []
    for index in range(5):
        case = deepcopy(template)
        case.update(
            id=f"q-{index}", entity_group=f"entity-{index}", query=template["query"] + str(index)
        )
        dataset["cases"].append(case)
    source, frozen = tmp_path / "source.json", tmp_path / "frozen.json"
    source.write_text(json.dumps(dataset))
    freeze_dataset(source, frozen)
    report = runner.run_experiment(frozen, tmp_path / "run", repeats=2)
    assert report["completed_jobs"] == 30
    for comparison in report["paired"]:
        assert comparison["entity_groups"] == comparison["paired_cases"] == 5
        assert comparison["families"][template["template_family"]]["cases"] == 5
    assert report["paired"][0]["entity_cluster_bootstrap_95"] == [-1, -1]
    assert report["paired"][1]["entity_cluster_bootstrap_95"] == [0, 0]
    assert all(c["assessment"] == "EXPLORATORY" for c in report["paired"])


def test_cli_returns_nonzero_on_incomplete_experiment(frozen, tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("PRIVATE")

    monkeypatch.setattr(runner, "retrieve", fail)
    output = tmp_path / "run"
    result = CliRunner().invoke(app, ["experiment-run", str(frozen), str(output)])
    assert result.exit_code == 1
    assert "INCOMPLETE" in result.output and "PRIVATE" not in result.output
    assert load_json(output / "report.json")["failed_jobs"] == 6


def test_reference_labels_do_not_enter_retrieval_context(frozen, monkeypatch):
    case = read_snapshot(frozen).dataset.cases[0]
    original, calls = runner.ContextCompiler.compile, []

    def inspect(self, principal, project, environment, query, **kwargs):
        assert query == case.query and set(kwargs) == {"budget"}
        bundle = original(self, principal, project, environment, query, **kwargs)
        assert all(
            "relevant" not in doc and "reference_answer" not in doc for doc in bundle.documents
        )
        calls.append(1)
        return bundle

    monkeypatch.setattr(runner.ContextCompiler, "compile", inspect)
    expected = runner.retrieve(case, "lexical")
    case.relevant = ["distractor"]
    assert runner.retrieve(case, "lexical") == expected
    assert len(calls) == 2
