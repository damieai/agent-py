"""Paired Harness execution, paid-call opt-in, accounting and durable non-replay."""

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from agent_py import investigation_experiments as runner
from agent_py.artifacts import ArtifactStore
from agent_py.cli import app
from agent_py.db import Database, Task
from agent_py.domain import digest
from agent_py.experiment_datasets import freeze_dataset
from agent_py.jsonio import load_json
from agent_py.service import Service

ROOT = Path(__file__).resolve().parents[1]


def write(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def inputs(tmp_path):
    snapshot, config = tmp_path / "dataset.json", tmp_path / "config.json"
    freeze_dataset(ROOT / "examples/investigation-experiment.json", snapshot)
    config.write_bytes((ROOT / "examples/investigation-experiment-config.json").read_bytes())
    return snapshot, config, tmp_path / "run"


def change_config(inputs, **values):
    config = load_json(inputs[1])
    config.update(values)
    write(inputs[1], config)


def records(output):
    return [load_json(p)["record"] for p in (output / "jobs").glob("*/result.json")]


def calls(monkeypatch):
    seen = []
    original = runner.synthetic_response

    def capture(request):
        seen.append(json.loads(request.content))
        return original(request)

    monkeypatch.setattr(runner, "synthetic_response", capture)
    return seen


def test_real_harness_pairs_accounting_isolation_and_completed_resume(inputs, monkeypatch):
    seen = calls(monkeypatch)
    monkeypatch.setenv("AGENT_DATABASE_URL", "sqlite:///must-not-open.db")
    monkeypatch.setenv("AGENT_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("AGENT_RELEASE_MANIFEST", "/does/not/exist")
    monkeypatch.setenv("AGENT_COLLECTION_MANIFEST", "/does/not/exist")
    monkeypatch.setenv("AGENT_ALLOW_CANDIDATE_EXECUTION", "true")
    result = runner.run_investigation_experiment(*inputs)
    assert result["execution_status"] == "COMPLETE"
    assert len(seen) == 6
    assert result["ledger_exposure_micro_usd"] == 300
    assert result["results"][0]["dispatched_calls"] == 2
    assert result["results"][1]["dispatched_calls"] == 4
    assert all(p["mean_ledger_cost_delta_micro_usd"] == 50 for p in result["paired"])
    assert all(r["operations"] == 0 for r in records(inputs[2]))
    assert result["task_success"] == result["answer_quality"] == "NOT_ASSESSED"
    assert result["provider_billing"] == "NOT_INCURRED"
    assert all("relevant" not in json.loads(p["messages"][0]["content"]) for p in seen)
    assert len({r["task_id"] for r in records(inputs[2])}) == 4
    before = {p: p.read_bytes() for p in (inputs[2] / "jobs").glob("*/result.json")}
    assert runner.run_investigation_experiment(*inputs, resume=True) == result
    assert len(seen) == 6
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize("allow,key", [(False, ""), (True, ""), (False, "private-key")])
def test_live_requires_both_explicit_paid_export_opt_in_and_credential(inputs, allow, key):
    change_config(inputs, mode="live", model_id="operator-selected-model")
    with pytest.raises(ValueError, match="opt-in"):
        runner.run_investigation_experiment(*inputs, allow_model_api=allow, api_key=key)
    assert not inputs[2].exists()


def test_live_path_uses_explicit_transport_without_proxy_or_credential_persistence(
    inputs, monkeypatch
):
    change_config(inputs, mode="live", model_id="operator-selected-model")
    seen = []

    def transport(**kwargs):
        assert kwargs == {"retries": 0, "trust_env": False}

        def respond(request):
            assert request.headers["x-api-key"] == "private-live-key"
            seen.append(request)
            return runner.synthetic_response(request)

        return httpx.MockTransport(respond)

    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    report = runner.run_investigation_experiment(
        *inputs, allow_model_api=True, api_key="private-live-key"
    )
    assert report["mode"] == "live"
    assert report["provider_billing"] == "NOT_RECONCILED"
    assert len(seen) == 6
    assert not any(
        b"private-live-key" in p.read_bytes() for p in inputs[2].rglob("*") if p.is_file()
    )


def test_total_budget_is_admitted_before_any_job(inputs, monkeypatch):
    seen = calls(monkeypatch)
    change_config(inputs, total_budget_micro_usd=399999)
    with pytest.raises(ValueError, match="both arms"):
        runner.run_investigation_experiment(*inputs)
    assert not seen and not inputs[2].exists()


def test_per_task_budget_exhaustion_does_not_send_model_request(inputs, monkeypatch):
    seen = calls(monkeypatch)
    change_config(inputs, task_budget_micro_usd=1)
    report = runner.run_investigation_experiment(*inputs)
    assert report["execution_status"] == "INCOMPLETE"
    assert not seen
    assert all(
        r["status"] == "BLOCKED" and r["reason"] in {"BUDGET_EXHAUSTED", "DAILY_BUDGET_EXHAUSTED"}
        for r in records(inputs[2])
    )
    assert report["ledger_exposure_micro_usd"] == 0


def test_artifact_interruption_recovers_validated_decision_without_rebilling(inputs, monkeypatch):
    seen = calls(monkeypatch)
    original = ArtifactStore.put
    interrupted = False

    def interrupt(self, *args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ArtifactStore, "put", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run_investigation_experiment(*inputs)
    assert len(seen) == 1
    report = runner.run_investigation_experiment(*inputs, resume=True)
    assert report["execution_status"] == "COMPLETE"
    assert len(seen) == 6
    assert report["results"][0]["recovered_latency_excluded"] == 1
    assert report["results"][0]["latency_samples"] == 1


def test_dispatched_unsettled_call_is_never_resent(inputs, monkeypatch):
    seen = calls(monkeypatch)
    original = Service.claim_inference
    interrupted = False

    def interrupt(self, *args, **kwargs):
        nonlocal interrupted
        result = original(self, *args, **kwargs)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Service, "claim_inference", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run_investigation_experiment(*inputs)
    assert not seen
    report = runner.run_investigation_experiment(*inputs, resume=True)
    assert report["execution_status"] == "INCOMPLETE"
    rows = records(inputs[2])
    unknown = next(r for r in rows if r["status"] == "UNKNOWN")
    assert unknown["reserved_micro_usd"] > 0
    assert unknown["calls"][0]["actual"] is None
    assert len(seen) == 5
    runner.run_investigation_experiment(*inputs, resume=True)
    assert len(seen) == 5


def test_timeout_is_conservatively_charged_and_no_automatic_retry(inputs, monkeypatch):
    seen = []

    def fail(request):
        seen.append(request)
        raise httpx.ReadTimeout("private provider error")

    monkeypatch.setattr(runner, "synthetic_response", fail)
    report = runner.run_investigation_experiment(*inputs)
    assert len(seen) == 4
    assert report["execution_status"] == "INCOMPLETE"
    assert all(
        r["status"] == "UNKNOWN" and r["calls"][0]["actual"] == r["calls"][0]["maximum"]
        for r in records(inputs[2])
    )
    assert "private provider error" not in json.dumps(records(inputs[2]))
    runner.run_investigation_experiment(*inputs, resume=True)
    assert len(seen) == 4


@pytest.mark.parametrize("fault", ["missing", "empty", "task", "document", "symlink"])
def test_broken_recovery_state_is_rejected_before_any_new_call(
    inputs, monkeypatch, fault, tmp_path
):
    seen = calls(monkeypatch)
    original = runner.execute_job

    def interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "execute_job", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run_investigation_experiment(*inputs)
    monkeypatch.setattr(runner, "execute_job", original)
    state = next((inputs[2] / "jobs").glob("*/state.db"))
    if fault == "missing":
        state.unlink()
    elif fault == "empty":
        state.write_bytes(b"")
    elif fault == "symlink":
        target = tmp_path / "original.db"
        state.rename(target)
        state.symlink_to(target)
    else:
        from agent_py.db import Document

        db = Database(f"sqlite:///{state}")
        with db.session(runner.TENANT) as s:
            if fault == "task":
                s.scalar(select(Task)).request_key = "other"
            else:
                s.scalar(select(Document)).body = "changed"
        db.engine.dispose()
    with pytest.raises((ValueError, OSError)):
        runner.run_investigation_experiment(*inputs, resume=True)
    assert len(seen) == 1


@pytest.mark.parametrize("fault", ["result", "analysis", "extra", "plan", "implementation"])
def test_corrupt_old_results_are_validated_before_pending_execution(inputs, monkeypatch, fault):
    seen = calls(monkeypatch)
    runner.run_investigation_experiment(*inputs)
    jobs = list((inputs[2] / "jobs").iterdir())
    # Deleting a final result makes it a recoverable pending job, not permission to rebill.
    (jobs[-1] / "result.json").unlink()
    if fault == "result":
        value = load_json(jobs[0] / "result.json")
        value["record"]["spent_micro_usd"] += 1
        value["content_digest"] = digest(value["record"])
        write(jobs[0] / "result.json", value)
    elif fault == "analysis":
        (jobs[0] / "analysis.json").write_text("{}")
    elif fault == "extra":
        (inputs[2] / "jobs" / "unknown").mkdir()
    elif fault == "plan":
        change_config(inputs, repeats=2, total_budget_micro_usd=800000)
    else:
        monkeypatch.setattr(runner, "implementation_identity", lambda: {"changed": True})
    with pytest.raises(ValueError):
        runner.run_investigation_experiment(*inputs, resume=True)
    assert len(seen) == 6


def test_provider_cost_overrun_stops_remaining_jobs(inputs, monkeypatch):
    original = runner.synthetic_response
    seen = []

    def overrun(request):
        seen.append(request)
        body = original(request).json()
        body["usage"]["input_tokens"] = 1_000_000
        return httpx.Response(200, json=body)

    monkeypatch.setattr(runner, "synthetic_response", overrun)
    result = runner.run_investigation_experiment(*inputs)
    assert result["execution_status"] == "INVALID"
    assert len(result["hard_gate_violations"]) == 1
    assert result["pending_jobs"] == 3
    assert len(seen) == 1
    runner.run_investigation_experiment(*inputs, resume=True)
    assert len(seen) == 1


def test_cli_defaults_offline_and_live_error_is_private(inputs):
    cli = CliRunner()
    result = cli.invoke(app, ["investigation-experiment", *map(str, inputs)])
    assert result.exit_code == 0, result.output
    assert "synthetic" in result.output
    change_config(inputs, mode="live", model_id="operator-selected-model")
    result = cli.invoke(
        app,
        ["investigation-experiment", *map(str, inputs)],
        env={"AGENT_EXPERIMENT_MODEL_API_KEY": "private-key"},
    )
    assert result.exit_code != 0
    assert "private-key" not in result.output


def test_selected_cases_must_match_harness_context_contract(inputs):
    value = load_json(inputs[0])
    value["dataset"]["cases"][0]["context_budget"] = 3000
    value["content_digest"] = digest(value["dataset"])
    write(inputs[0], value)
    with pytest.raises(ValueError, match="6000"):
        runner.run_investigation_experiment(*inputs)
    assert not inputs[2].exists()


def test_original_task_deadline_is_not_extended_on_resume(inputs, monkeypatch):
    from datetime import timedelta

    from agent_py.db import now

    original = ArtifactStore.put

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ArtifactStore, "put", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run_investigation_experiment(*inputs)
    monkeypatch.setattr(ArtifactStore, "put", original)
    state = next((inputs[2] / "jobs").glob("*/state.db"))
    db = Database(f"sqlite:///{state}")
    with db.session(runner.TENANT) as s:
        s.scalar(select(Task)).deadline = now() - timedelta(seconds=1)
    db.engine.dispose()
    report = runner.run_investigation_experiment(*inputs, resume=True)
    assert report["execution_status"] == "INCOMPLETE"
    assert any(r["reason"] == "DEADLINE" for r in records(inputs[2]))


def test_resume_between_loop_rounds_reuses_original_round_and_finishes(inputs, monkeypatch):
    from agent_py.investigation import InvestigationHarness

    seen = calls(monkeypatch)
    original = InvestigationHarness.tick
    interrupted = False

    def interrupt(self, *args, **kwargs):
        nonlocal interrupted
        result = original(self, *args, **kwargs)
        if result.get("wait") == "INVESTIGATION_CONTINUE" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(InvestigationHarness, "tick", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run_investigation_experiment(*inputs)
    assert len(seen) == 2
    report = runner.run_investigation_experiment(*inputs, resume=True)
    assert report["execution_status"] == "COMPLETE"
    assert len(seen) == 6
    assert report["results"][1]["recovered_latency_excluded"] == 1


def test_raw_analysis_must_match_ledger_even_if_artifact_hash_rewritten(inputs):
    runner.run_investigation_experiment(*inputs)
    path = next((inputs[2] / "jobs").glob("*/result.json"))
    envelope = load_json(path)
    artifact_path = path.parent / "analysis.json"
    analysis = load_json(artifact_path)
    row = analysis.get("rounds", [analysis])[0]
    row["decision"]["summary"] = "altered answer"
    write(artifact_path, analysis)
    envelope["record"]["analysis_digest"] = digest(analysis)
    envelope["content_digest"] = digest(envelope["record"])
    write(path, envelope)
    with pytest.raises(ValueError, match="ledger"):
        runner.run_investigation_experiment(*inputs, resume=True)


def test_no_evidence_is_missing_output_not_model_success(inputs, monkeypatch):
    seen = calls(monkeypatch)
    snapshot = load_json(inputs[0])
    for case in snapshot["dataset"]["cases"]:
        case["query"] = "zzzzzzz nonexistent corpus match " + case["id"].replace("-", "")
    snapshot["content_digest"] = digest(snapshot["dataset"])
    write(inputs[0], snapshot)
    report = runner.run_investigation_experiment(*inputs)
    assert report["execution_status"] == "INCOMPLETE"
    assert not seen
    assert all(r["reason"] == "EVIDENCE_REQUIRED" for r in records(inputs[2]))


def test_source_change_during_run_invalidates_report(inputs, monkeypatch):
    identity = runner.implementation_identity()
    count = 0

    def changed():
        nonlocal count
        count += 1
        return identity if count == 1 else {"changed": True}

    monkeypatch.setattr(runner, "implementation_identity", changed)
    report = runner.run_investigation_experiment(*inputs)
    assert report["execution_status"] == "INVALID"
    assert report["error"] == "IMPLEMENTATION_CHANGED"


def test_malformed_analysis_envelope_is_rejected_safely(inputs):
    runner.run_investigation_experiment(*inputs)
    path = next((inputs[2] / "jobs").glob("*/result.json"))
    envelope = load_json(path)
    write(path.parent / "analysis.json", [])
    envelope["record"]["analysis_digest"] = digest([])
    envelope["content_digest"] = digest(envelope["record"])
    write(path, envelope)
    result = CliRunner().invoke(app, ["investigation-experiment", *map(str, inputs), "--resume"])
    assert result.exit_code == 2
    assert "Invalid value" in result.output
