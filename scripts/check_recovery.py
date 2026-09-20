"""Run isolated Linux process-death drills and require all four native scenarios to pass."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

SCENARIOS = {
    "before_remote_commit",
    "after_remote_commit",
    "after_local_commit",
    "temporal_restart",
}
TESTS = {
    "test_hard_kill_action_boundary[before_remote_commit]",
    "test_hard_kill_action_boundary[after_remote_commit]",
    "test_hard_kill_action_boundary[after_local_commit]",
    "test_temporal_worker_process_restart_and_history_replay",
}
ROOT = Path(__file__).resolve().parents[1]


def inputs():
    paths = list((ROOT / "src/agent_py").rglob("*.py")) + [
        ROOT / "tests/test_process_recovery.py",
        ROOT / "tests/support/recovery_worker.py",
        ROOT / "scripts/check_recovery.py",
        ROOT / "uv.lock",
    ]
    return {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)
    }


def assess(directory, exit_code):
    errors, cases = [], {}
    if exit_code != 0:
        errors.append(f"pytest_exit:{exit_code}")
    try:
        xml = ET.parse(directory / "junit.xml")
        tests = list(xml.iter("testcase"))
        if {test.get("name") for test in tests} != TESTS or len(tests) != len(TESTS):
            errors.append("test_set_mismatch")
        if any(
            test.find(tag) is not None for test in tests for tag in ("skipped", "error", "failure")
        ):
            errors.append("test_failed_or_skipped")
    except (OSError, ET.ParseError):
        errors.append("junit_missing_or_invalid")
    try:
        history = json.loads((directory / "temporal-history.json").read_text())
        if not isinstance(history.get("events"), list) or not history["events"]:
            errors.append("history_empty_or_invalid")
    except (OSError, ValueError, AttributeError):
        errors.append("history_missing_or_invalid")
    for name in sorted(SCENARIOS):
        try:
            data = json.loads((directory / f"{name}.json").read_text())
            if data.get("passed") is not True or data.get("scenario") != name:
                errors.append(f"scenario_invalid:{name}")
            cases[name] = data
        except (OSError, ValueError, AttributeError):
            errors.append(f"scenario_missing_or_invalid:{name}")
    return {"status": "FAIL" if errors else "PASS", "errors": errors, "scenarios": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".runtime/recovery")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Recovery drills require Linux SIGKILL/process groups")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir.resolve()))
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
    env.update(AGENT_TEST_RECOVERY="1", AGENT_RECOVERY_REPORT_DIR=str(directory))
    # Do not accept ambient pytest selection/skip/plugin settings as verification evidence.
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    started = time.monotonic()
    source_hashes = inputs()
    with subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_process_recovery.py",
            "-q",
            "-o",
            "faulthandler_timeout=120",
            f"--junitxml={directory / 'junit.xml'}",
        ],
        cwd=ROOT,
        env=env,
        start_new_session=True,
    ) as process:
        try:
            exit_code = process.wait(timeout=180)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            # This group contains only this drill's pytest, workers and local server.
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            exit_code = 124
        finally:
            # A crashed pytest can leave children even when it exits before the deadline.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    report = {
        "schema": "process-recovery/v1",
        **assess(directory, exit_code),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "production_acceptance": False,
        "scope": "Linux_SIGKILL_SQLite_simulated_authority_and_native_Temporal_same_version",
        "inputs_sha256": source_hashes,
    }
    if inputs() != source_hashes:
        report["status"] = "FAIL"
        report["errors"].append("source_changed_during_drill")
    report["evidence_sha256"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    print(f"Recovery drill: {report['status']}; report: {directory / 'report.json'}", flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
