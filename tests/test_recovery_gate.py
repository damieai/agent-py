"""A skipped/partial drill or stale report must never produce a passing gate."""

import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "recovery_gate", Path(__file__).resolve().parents[1] / "scripts/check_recovery.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def seed(root):
    (root / "temporal-history.json").write_text('{"events":[{"eventId":"1"}]}')
    suite = ET.Element("testsuite")
    for name in sorted(gate.TESTS):
        ET.SubElement(suite, "testcase", name=name)
    ET.ElementTree(suite).write(root / "junit.xml")
    for name in gate.SCENARIOS:
        (root / f"{name}.json").write_text(json.dumps({"scenario": name, "passed": True}))
    return suite


@pytest.mark.parametrize("content", [None, '{"events":[]}'])
def test_missing_or_empty_history_blocks_gate(tmp_path, content):
    seed(tmp_path)
    path = tmp_path / "temporal-history.json"
    if content is None:
        path.unlink()
    else:
        path.write_text(content)
    assert gate.assess(tmp_path, 0)["status"] == "FAIL"


def test_complete_drill_passes(tmp_path):
    seed(tmp_path)
    result = gate.assess(tmp_path, 0)
    assert result["status"] == "PASS" and result["errors"] == []


@pytest.mark.parametrize(
    "failure", ["skipped", "failure", "error", "missing_test", "duplicate", "wrong_test"]
)
def test_junit_cannot_skip_or_substitute_scenarios(tmp_path, failure):
    suite = seed(tmp_path)
    if failure == "missing_test":
        suite.remove(suite[0])
    elif failure == "duplicate":
        ET.SubElement(suite, "testcase", name=suite[0].get("name"))
    elif failure == "wrong_test":
        suite[0].set("name", "unrelated")
    else:
        ET.SubElement(suite[0], failure)
    ET.ElementTree(suite).write(tmp_path / "junit.xml")
    assert gate.assess(tmp_path, 0)["status"] == "FAIL"


@pytest.mark.parametrize(
    "failure", ["missing", "invalid_json", "false", "wrong_scenario", "non_object", "exit", "xml"]
)
def test_missing_or_invalid_evidence_fails_closed(tmp_path, failure):
    seed(tmp_path)
    file = tmp_path / "temporal_restart.json"
    if failure == "missing":
        file.unlink()
    elif failure == "invalid_json":
        file.write_text("{")
    elif failure == "false":
        file.write_text('{"scenario":"temporal_restart","passed":false}')
    elif failure == "wrong_scenario":
        file.write_text('{"scenario":"different","passed":true}')
    elif failure == "non_object":
        file.write_text("[]")
    elif failure == "xml":
        (tmp_path / "junit.xml").write_text("<broken")
    assert gate.assess(tmp_path, 1 if failure == "exit" else 0)["status"] == "FAIL"
