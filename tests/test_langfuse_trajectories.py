"""Actual three-workflow rehearsal plus negative controls for its acceptance oracle."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

pytest.importorskip("langfuse")
spec = importlib.util.spec_from_file_location(
    "trajectory_probe",
    Path(__file__).resolve().parents[1] / "scripts/check_langfuse_trajectories.py",
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    directory = tmp_path_factory.mktemp("trajectories")
    # Host settings must never opt the child into real platform/model traffic.
    environment = dict(
        os.environ,
        AGENT_LANGFUSE_BASE_URL="https://unexpected.invalid",
        AGENT_MODEL_API_KEY="PRIVATE_INHERITED_KEY",
        AGENT_CONTEXT_STRATEGY="invalid",
        OTEL_SDK_DISABLED="true",
    )
    child = subprocess.run(
        [sys.executable, probe.__file__, "--output-dir", str(directory)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=90,
    )
    assert child.returncode == 0, child.stdout + child.stderr
    path = next(directory.glob("run-*/report.json"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert "PRIVATE" not in path.read_text()
    return json.loads(path.read_text())


def test_real_harness_rehearsal_is_complete_and_does_not_claim_platform_acceptance(report):
    assert report["schema_version"] == 2
    assert report["correctness"] == "PASS"
    assert report["platform"] == report["real_model"] == report["real_sandbox"] == "NOT_RUN"
    assert report["performance"] == "NOT_ASSESSED"
    assert not report["errors"]
    assert {(c["workflow"], c["mode"]) for c in report["cases"]} == {
        (w, m) for w in probe.WORKFLOWS for m in probe.MODES
    }
    assert "uv.lock" in report["inputs"]
    for case in report["cases"]:
        assert probe.validate_case(case) == []


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("duplicate_generation", "generation_count"),
        ("missing_parent", "parent_missing"),
        ("missing_origin", "origin_missing"),
        ("missing_previous", "previous_tick_missing"),
        ("wrong_session", "session_identity"),
        ("double_cost", "reuse_double_count"),
        ("wrong_cost", "export_cost"),
        ("shifted_cost", "generation_cost"),
        ("extra_delivery", "delivery_accounting"),
        ("wrong_ledger", "ledger_cost"),
        ("extra_model_call", "model_call_count"),
        ("replay_changed", "recovery_reuse"),
        ("privacy_leak", "privacy"),
        ("export_lost", "delivery_count"),
        ("business_success", "business_completion"),
    ],
)
def test_negative_controls_cannot_pass(report, fault, expected):
    case = deepcopy(
        next(
            c
            for c in report["cases"]
            if c["workflow"] == "investigation_loop" and c["mode"] == "healthy"
        )
    )
    generation = next(s for s in case["spans"] if s["name"] == "model.generation")
    reused = next(s for s in case["spans"] if s["name"] == "model.result_reused")
    if fault == "duplicate_generation":
        case["spans"].append(deepcopy(generation))
    elif fault == "missing_parent":
        generation["parent"] = "0" * 16
    elif fault == "missing_origin":
        next(s for s in case["spans"] if s["name"] == "worker.tick")["links"] = []
    elif fault == "missing_previous":
        tick = [s for s in case["spans"] if s["name"] == "worker.tick"][-1]
        tick["links"] = [link for link in tick["links"] if link["role"] != "previous"]
    elif fault == "wrong_session":
        generation["session"] = "b" * 64
    elif fault == "double_cost":
        reused["cost"] = {"input": 0.1}
    elif fault == "wrong_cost":
        generation["cost"]["input"] = 1
    elif fault == "shifted_cost":
        generations = [s for s in case["spans"] if s["name"] == "model.generation"]
        generations[0]["cost"]["input"] = 0.00004
        generations[1]["cost"]["input"] = 0.00002
    elif fault == "extra_delivery":
        case["counters"]["export_failed"] = 1
    elif fault == "wrong_ledger":
        case["business"]["ledger_actual"] += 1
    elif fault == "extra_model_call":
        case["business"]["model_calls"] += 1
    elif fault == "replay_changed":
        case["business"]["repeat_stable"] = False
    elif fault == "privacy_leak":
        case["privacy_clean"] = False
    elif fault == "export_lost":
        case["counters"]["exported"] -= 1
    elif fault == "business_success":
        case["business"]["result"] = "SUCCESS"
    assert expected in probe.validate_case(case)


@pytest.mark.parametrize("failure", ["timeout", "incomplete"])
def test_missing_child_evidence_writes_failed_report(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(sys, "argv", ["probe", "--output-dir", str(tmp_path)])

    def child(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("synthetic", 45)
        return subprocess.CompletedProcess([], 0, stdout='{"correctness":"PASS"}')

    monkeypatch.setattr(probe.subprocess, "run", child)
    with pytest.raises(SystemExit) as exc:
        probe.main()
    assert exc.value.code == 1
    report = json.loads(next(tmp_path.glob("run-*/report.json")).read_text())
    assert report["correctness"] == "FAIL"
    assert len(report["errors"]) == 12
    assert report["platform"] == "NOT_RUN"


def test_changed_inputs_cannot_pass(report, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["probe", "--output-dir", str(tmp_path)])
    versions = iter([{"script": "before"}, {"script": "after"}])
    monkeypatch.setattr(probe, "inputs", lambda: next(versions))

    def child(command, **kwargs):
        workflow, mode = command[command.index("--case") + 1], command[-1]
        case = next(c for c in report["cases"] if c["workflow"] == workflow and c["mode"] == mode)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(case))

    monkeypatch.setattr(probe.subprocess, "run", child)
    with pytest.raises(SystemExit) as exc:
        probe.main()
    assert exc.value.code == 1
    result = json.loads(next(tmp_path.glob("run-*/report.json")).read_text())
    assert result["errors"] == ["inputs_changed"]


@pytest.mark.parametrize(
    "field,error",
    [("candidate_id", "candidate_identity"), ("verification_id", "verification_identity")],
)
def test_repair_lineage_mismatch_cannot_pass(report, field, error):
    case = deepcopy(
        next(
            c
            for c in report["cases"]
            if c["workflow"] == "repair_candidate" and c["mode"] == "healthy"
        )
    )
    result = next(s for s in case["spans"] if s["name"] == "verification.result")
    result["workflow_metadata"][field] = "f" * 64
    assert error in probe.validate_case(case)
