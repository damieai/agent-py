"""Archived evidence is independently checked; failure curation never promotes holdout."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_py import retrieval_experiments as runner
from agent_py.cli import app
from agent_py.domain import digest
from agent_py.experiment_datasets import freeze_dataset, read_snapshot
from agent_py.experiment_review import audit_experiment, curate_experiment
from agent_py.jsonio import load_json

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/retrieval-experiment.json"


def write(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def archive(tmp_path, monkeypatch):
    snapshot = tmp_path / "frozen.json"
    freeze_dataset(EXAMPLE, snapshot)
    monkeypatch.setattr(
        runner,
        "retrieve",
        lambda *_: dict(retrieved=[], context_digest=digest([]), context_bytes=0, chunks=0),
    )
    output = tmp_path / "run"
    runner.run_experiment(snapshot, output)
    return output


def review_for(archive, path):
    audit = audit_experiment(archive)
    review = {
        "format": "agent-experiment-curation/v1",
        "evidence_digest": audit["evidence_digest"],
        "dataset_name": "reviewed-failures",
        "decisions": [
            {
                "case_id": audit["review_queue"][0]["case_id"],
                "reviewer": "reviewer-1",
                "rubric_version": "relevance-v1",
                "reference_verified": True,
                "redaction_verified": True,
            }
        ],
    }
    write(path, review)
    return review


def test_audit_is_read_only_and_independent_of_current_runner(archive, monkeypatch):
    before = {
        p.relative_to(archive): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in archive.rglob("*")
        if p.is_file()
    }

    def forbidden(*args, **kwargs):
        pytest.fail("Audit must not run retrieval or inspect current implementation")

    monkeypatch.setattr(runner, "retrieve", forbidden)
    monkeypatch.setattr(runner, "implementation_identity", forbidden)
    result = audit_experiment(archive)
    assert result["audit_status"] == "COMPLETE"
    assert len(result["review_queue"]) == 2
    assert all(len(r["failures"]) == 2 for r in result["review_queue"])
    assert result["recomputed_report"] == load_json(archive / "report.json")
    after = {
        p.relative_to(archive): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in archive.rglob("*")
        if p.is_file()
    }
    assert before == after


@pytest.mark.parametrize(
    "fault", ["implementation", "plan", "record", "unknown", "extra", "symlink", "duplicate"]
)
def test_audit_rejects_corrupt_evidence(archive, fault, tmp_path):
    path = archive / "plan.json"
    data = load_json(path)
    if fault == "implementation":
        data["implementation"]["python"] = "changed"
    elif fault == "plan":
        data["plan"]["dataset_digest"] = "0" * 64
    elif fault in {"record", "unknown", "symlink"}:
        path = next((archive / "records").glob("*.json"))
        data = load_json(path)
        if fault == "symlink":
            target = tmp_path / "linked.json"
            target.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(target)
            with pytest.raises(ValueError):
                audit_experiment(archive)
            return
        data["record"]["retrieved"] = ["unknown"]
        if fault == "unknown":
            data["content_digest"] = digest(data["record"])
    elif fault == "extra":
        path = archive / "records" / "unexpected.json"
    elif fault == "duplicate":
        path.write_text('{"plan": {}, "plan": {}}')
        with pytest.raises(ValueError):
            audit_experiment(archive)
        return
    write(path, data)
    with pytest.raises(ValueError):
        audit_experiment(archive)


@pytest.mark.parametrize(
    "fault,status",
    [
        ("missing", "MISSING"),
        ("tampered", "MISMATCH"),
        ("invalid", "MISMATCH"),
        ("broken", "UNREADABLE"),
    ],
)
def test_report_is_not_a_trust_root(archive, fault, status):
    path = archive / "report.json"
    if fault == "missing":
        path.unlink()
    elif fault == "broken":
        path.write_text("not json")
    else:
        data = load_json(path)
        if fault == "invalid":
            data["execution_status"] = "INVALID"
            data["error"] = "IMPLEMENTATION_CHANGED"
        else:
            data["results"][0]["metrics"]["recall"] = 1
        write(path, data)
    result = audit_experiment(archive)
    assert result["report_status"] == status
    assert result["audit_status"] == "REPORT_MISMATCH"
    assert result["recomputed_report"]["results"][0]["metrics"]["recall"] == 0


def test_missing_record_changes_identity_and_does_not_get_reexecuted(archive):
    before = audit_experiment(archive)
    next((archive / "records").glob("*.json")).unlink()
    after = audit_experiment(archive)
    assert after["evidence_digest"] != before["evidence_digest"]
    assert after["recomputed_report"]["pending_jobs"] == 1
    assert after["recomputed_report"]["execution_status"] == "INCOMPLETE"
    assert after["report_status"] == "MISMATCH"


def test_writer_lock_blocks_audit_and_curation(archive, tmp_path):
    path = tmp_path / "review.json"
    review_for(archive, path)
    with runner.exclusive_run(archive):
        with pytest.raises(ValueError, match="written"):
            audit_experiment(archive)
        with pytest.raises(ValueError, match="written"):
            curate_experiment(archive, path, tmp_path / "curated")


def test_curated_snapshot_preserves_case_and_lineage(archive, tmp_path):
    request = tmp_path / "review.json"
    review = review_for(archive, request)
    output = tmp_path / "curated"
    lineage = curate_experiment(archive, request, output)
    snapshot = read_snapshot(output / "dataset.json")
    original = read_snapshot(archive / "dataset.json")
    assert snapshot.dataset.cases == [
        c for c in original.dataset.cases if c.id == review["decisions"][0]["case_id"]
    ]
    assert all(c.split == "development" for c in snapshot.dataset.cases)
    assert lineage["review_digest"] == digest(review)
    assert lineage["source_evidence_digest"] == audit_experiment(archive)["evidence_digest"]
    assert load_json(output / "complete.json") == {
        "dataset_digest": snapshot.content_digest,
        "lineage_digest": digest(lineage),
    }
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in output.iterdir())
    with pytest.raises(FileExistsError):
        curate_experiment(archive, request, output)


@pytest.mark.parametrize(
    "fault",
    ["stale", "missing_review", "false_review", "numeric_review", "unknown", "duplicate", "report"],
)
def test_curation_rejects_unreviewed_or_changed_inputs(archive, tmp_path, fault):
    path = tmp_path / "review.json"
    review = review_for(archive, path)
    decision = review["decisions"][0]
    if fault == "stale":
        review["evidence_digest"] = "0" * 64
    elif fault == "missing_review":
        del decision["reference_verified"]
    elif fault == "false_review":
        decision["redaction_verified"] = False
    elif fault == "numeric_review":
        decision["reference_verified"] = 1
    elif fault == "unknown":
        decision["case_id"] = "unknown"
    elif fault == "duplicate":
        review["decisions"].append(decision.copy())
    else:
        (archive / "report.json").unlink()
    write(path, review)
    output = tmp_path / "curated"
    with pytest.raises(ValueError):
        curate_experiment(archive, path, output)
    assert not output.exists()


@pytest.mark.parametrize("split", ["calibration", "holdout"])
def test_non_development_failure_cannot_be_promoted(archive, tmp_path, split):
    output = tmp_path / split
    runner.run_experiment(archive / "dataset.json", output, split=split)
    review = tmp_path / "review.json"
    review_for(output, review)
    assert all(not r["eligible_for_development"] for r in audit_experiment(output)["review_queue"])
    with pytest.raises(ValueError, match="development"):
        curate_experiment(output, review, tmp_path / "curated")


def test_none_baseline_alone_is_not_a_failure_source(archive, monkeypatch, tmp_path):
    def retrieve(case, strategy):
        if strategy == "none":
            return dict(retrieved=[], context_digest=digest([]), context_bytes=0, chunks=0)
        return dict(
            retrieved=case.relevant,
            context_digest=digest([]),
            context_bytes=1,
            chunks=len(case.relevant),
        )

    monkeypatch.setattr(runner, "retrieve", retrieve)
    output = tmp_path / "healthy"
    runner.run_experiment(archive / "dataset.json", output)
    assert audit_experiment(output)["review_queue"] == []
    request = tmp_path / "review.json"
    review = review_for(archive, request)
    review["evidence_digest"] = audit_experiment(output)["evidence_digest"]
    write(request, review)
    with pytest.raises(ValueError, match="failures"):
        curate_experiment(output, request, tmp_path / "curated")


def test_failed_jobs_remain_incomplete_and_are_reviewable(archive, monkeypatch, tmp_path):
    def failed(*_):
        raise RuntimeError("private-exception")

    monkeypatch.setattr(runner, "retrieve", failed)
    output = tmp_path / "failed"
    runner.run_experiment(archive / "dataset.json", output)
    result = audit_experiment(output)
    assert result["audit_status"] == "INCOMPLETE"
    assert result["recomputed_report"]["failed_jobs"] == 6
    assert "private-exception" not in json.dumps(result)
    review = tmp_path / "review.json"
    review_for(output, review)
    curate_experiment(output, review, tmp_path / "curated")


def test_cli_audit_curation_and_safe_errors(archive, tmp_path):
    cli = CliRunner()
    audit = tmp_path / "audit.json"
    assert cli.invoke(app, ["experiment-audit", str(archive), str(audit)]).exit_code == 0
    assert cli.invoke(app, ["experiment-audit", str(archive), str(audit)]).exit_code != 0
    request = tmp_path / "review.json"
    review_for(archive, request)
    assert (
        cli.invoke(
            app, ["experiment-curate", str(archive), str(request), str(tmp_path / "curated")]
        ).exit_code
        == 0
    )
    assert (
        cli.invoke(app, ["experiment-audit", str(archive), str(archive / "audit.json")]).exit_code
        != 0
    )
    assert not (archive / "audit.json").exists()
    (archive / "report.json").unlink()
    result = cli.invoke(app, ["experiment-audit", str(archive), str(tmp_path / "mismatch.json")])
    assert result.exit_code == 1
    assert load_json(tmp_path / "mismatch.json")["report_status"] == "MISSING"
    (archive / "plan.json").write_text("secret-invalid-json")
    result = cli.invoke(app, ["experiment-audit", str(archive), str(tmp_path / "invalid.json")])
    assert result.exit_code != 0
    assert "secret-invalid-json" not in result.output
    assert not (tmp_path / "invalid.json").exists()


def test_independent_evidence_pin_detects_rewritten_ledger(archive):
    pin = audit_experiment(archive)["evidence_digest"]
    assert audit_experiment(archive, expected_evidence_digest=pin)["evidence_digest"] == pin
    path = next((archive / "records").glob("*.json"))
    envelope = load_json(path)
    envelope["record"]["elapsed_ns"] += 1
    envelope["content_digest"] = digest(envelope["record"])
    write(path, envelope)
    with pytest.raises(ValueError, match="pin mismatch"):
        audit_experiment(archive, expected_evidence_digest=pin)
