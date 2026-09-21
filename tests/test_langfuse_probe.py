"""The loopback probe must fail closed on missing child evidence."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("langfuse")
spec = importlib.util.spec_from_file_location(
    "langfuse_probe", Path(__file__).resolve().parents[1] / "scripts/check_langfuse.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_real_loopback_fault_probe(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(Path(probe.__file__)),
            "--tasks",
            "8",
            "--repeats",
            "1",
            "--output-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(next(tmp_path.glob("run-*/report.json")).read_text())
    assert report["correctness"] == "PASS"
    assert report["performance"] == "NOT_ASSESSED"
    assert {case["mode"] for case in report["cases"]} == set(probe.MODES)
    assert len(report["comparisons"]) == 4
    assert all(case["tasks"] == 8 and not case["failures"] for case in report["cases"])
    assert "uv.lock" in report["inputs"]


def test_missing_child_cannot_pass_any_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe",
            "--tasks",
            "8",
            "--repeats",
            "1",
            "--output-dir",
            str(tmp_path),
            "--max-added-p95-ms",
            "10",
        ],
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("fixture", 1)

    monkeypatch.setattr(probe.subprocess, "run", timeout)
    with pytest.raises(SystemExit) as exc:
        probe.main()
    assert exc.value.code == 1
    report = json.loads(next(tmp_path.glob("run-*/report.json")).read_text())
    assert report["correctness"] == report["performance"] == "FAIL"
    assert "0:incomplete_cases" in report["errors"]


def test_explicit_performance_threshold_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe",
            "--tasks",
            "8",
            "--repeats",
            "1",
            "--output-dir",
            str(tmp_path),
            "--max-added-p95-ms",
            "0",
        ],
    )

    def measured(args, **kwargs):
        mode = args[args.index("--case") + 1]
        result = dict(mode=mode, p95_ms=1 if mode == "disabled" else 2, failures=[])
        return subprocess.CompletedProcess(args, 0, json.dumps(result), "")

    monkeypatch.setattr(probe.subprocess, "run", measured)
    with pytest.raises(SystemExit) as exc:
        probe.main()
    assert exc.value.code == 1
    report = json.loads(next(tmp_path.glob("run-*/report.json")).read_text())
    assert report["correctness"] == "PASS"
    assert report["performance"] == "FAIL"
