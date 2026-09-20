import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py.formal_check import CASES, classify, run_suite, sha256

SAFE = """Model checking completed. No error has been found.
39 states generated, 14 distinct states found, 0 states left on queue.
Finished in 00s
"""
UNSAFE = """Error: Invariant NoWrongRelease is violated.
Error: The behavior up to this point is:
State 1: <Initial predicate>
State 2: <Release line 23, col 15 of module WorkerLease>
29 states generated, 17 distinct states found, 7 states left on queue.
Finished in 00s
"""


@pytest.mark.parametrize(
    "code,output,invariant,expected",
    [
        (0, SAFE, None, "PASS"),
        (12, UNSAFE, "NoWrongRelease", "PASS"),
        (12, UNSAFE, "NoStaleDispatch", "FAIL"),
        (0, UNSAFE, "NoWrongRelease", "FAIL"),
        (75, UNSAFE, "NoWrongRelease", "FAIL"),
        (12, UNSAFE.replace("State 2:", "Incomplete:"), "NoWrongRelease", "FAIL"),
        (12, UNSAFE + "Error: Tool crashed\n", "NoWrongRelease", "FAIL"),
        (12, UNSAFE.replace("Finished in", "Interrupted in"), "NoWrongRelease", "FAIL"),
        (0, SAFE + "Error: Tool crashed\n", None, "FAIL"),
        (1, SAFE, None, "FAIL"),
        (0, "", None, "FAIL"),
        (0, SAFE.replace("states generated", "partial states"), None, "FAIL"),
        (0, SAFE, "NoWrongRelease", "FAIL"),
    ],
)
def test_tlc_result_is_not_just_a_nonzero_exit(code, output, invariant, expected):
    assert classify(code, output, invariant)["status"] == expected


def fixture_tools(tmp_path):
    root = tmp_path / "repo"
    formal = root / "formal"
    formal.mkdir(parents=True)
    jar = tmp_path / "fake.jar"
    jar.write_bytes(b"trusted test double")
    (formal / "toolchain.json").write_text(json.dumps({"sha256": sha256(jar), "version": "TLC"}))
    for config, model, _ in CASES:
        (formal / f"{config}.cfg").write_text("test configuration")
        (formal / f"{model}.tla").write_text("test model")
    return root, jar, tmp_path / "output"


def test_hash_mismatch_never_executes_and_invalidates_previous_pass(tmp_path, monkeypatch):
    root, jar, output = fixture_tools(tmp_path)
    output.mkdir()
    (output / "report.json").write_text('{"status":"PASS"}')
    jar.write_bytes(b"tampered")

    def forbidden(*args, **kwargs):
        pytest.fail("untrusted jar executed")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="SHA-256"):
        run_suite(root, jar, "java", output)
    assert json.loads((output / "report.json").read_text())["status"] == "FAIL"


@pytest.mark.parametrize("failure", ["timeout", "syntax", "unexpected_success"])
def test_runner_fails_closed_and_preserves_per_case_evidence(tmp_path, monkeypatch, failure):
    root, jar, output = fixture_tools(tmp_path)
    calls = []

    def fake_run(command, **kwargs):
        if command == ["java", "-version"]:
            return SimpleNamespace(stdout="", stderr="test java")
        calls.append(command)
        assert kwargs["cwd"] != root
        assert command[command.index("-workers") + 1] == "1"
        assert kwargs["timeout"] == 2
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 2)
        kwargs["stdout"].write(
            "TLC\n" + (SAFE if failure == "unexpected_success" else "syntax error")
        )
        return SimpleNamespace(returncode=0 if failure == "unexpected_success" else 150)

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = run_suite(root, jar, "java", output, timeout=2)
    assert len(calls) == len(CASES)
    assert report["status"] == "FAIL"
    assert json.loads((output / "report.json").read_text()) == report
    assert len(list(output.glob("*.log"))) == len(CASES)


def test_checked_in_evidence_matches_models():
    formal = Path(__file__).resolve().parents[1] / "formal"
    report = json.loads((formal / "verification.json").read_text())
    assert report["status"] == "PASS"
    for name, digest in report["inputs"].items():
        assert sha256(formal / name) == digest, "Model changed: rerun TLC and refresh evidence"
    assert [(r["case"], r["expected_invariant"]) for r in report["cases"]] == [
        (config, invariant) for config, _, invariant in CASES
    ]
    assert all(r["status"] == "PASS" for r in report["cases"])
