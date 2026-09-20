"""Reproducible, finite TLC safety checks; never a production release authorization."""

import hashlib
import json
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

CASES = (
    ("Action", "Action", None),
    ("UnsafeRemote", "Action", "AtMostOneEffect"),
    ("WorkerLease", "WorkerLease", None),
    ("UnsafeDispatch", "WorkerLease", "NoStaleDispatch"),
    ("UnsafeRelease", "WorkerLease", "NoWrongRelease"),
    ("UnsafeCompletion", "WorkerLease", "NoStaleCompletion"),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify(returncode: int, output: str, invariant: str | None) -> dict:
    errors = re.findall(r"^Error: (.+)$", output, re.MULTILINE)
    trace = re.findall(r"^State \d+: <([^>]+)>", output, re.MULTILINE)
    counts = re.search(r"([\d,]+) states generated, ([\d,]+) distinct states found", output)
    finished = "Finished in " in output
    if invariant is None:
        passed = (
            returncode == 0
            and not errors
            and "Model checking completed. No error has been found." in output
            and counts is not None
            and finished
        )
    else:
        passed = (
            returncode == 12
            and errors
            == [f"Invariant {invariant} is violated.", "The behavior up to this point is:"]
            and len(trace) >= 2
            and counts is not None
            and finished
        )
    return {
        "status": "PASS" if passed else "FAIL",
        "returncode": returncode,
        "expected_invariant": invariant,
        "generated": int(counts[1].replace(",", "")) if counts else None,
        "distinct": int(counts[2].replace(",", "")) if counts else None,
        "trace_actions": [item.split(" line ")[0] for item in trace],
    }


def run_suite(root: Path, jar: Path, java: str, output_dir: Path, timeout: int = 60) -> dict:
    root, jar, output_dir = root.resolve(), jar.resolve(), output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps({"schema_version": 1, "status": "FAIL", "reason": "INCOMPLETE"}) + "\n"
    )
    manifest_path = root / "formal/toolchain.json"
    manifest = json.loads(manifest_path.read_text())
    if sha256(jar) != manifest["sha256"]:
        raise ValueError("TLC jar SHA-256 does not match formal/toolchain.json")
    if timeout <= 0:
        raise ValueError("Timeout must be positive")
    version = subprocess.run(
        [java, "-version"], capture_output=True, text=True, timeout=10, check=True
    )
    inputs = {"toolchain.json": sha256(manifest_path)}
    results = []
    for config, model, invariant in CASES:
        for name in (f"{config}.cfg", f"{model}.tla"):
            inputs[name] = sha256(root / "formal" / name)
        # TLC creates auxiliary files: isolate both cwd and state storage per configuration.
        with TemporaryDirectory(prefix="agent-tlc-") as temporary:
            command = [
                java,
                "-Xmx512m",
                "-XX:+UseParallelGC",
                "-cp",
                str(jar),
                "tlc2.TLC",
                "-workers",
                "1",
                "-seed",
                "1",
                "-fp",
                "0",
                "-metadir",
                str(Path(temporary) / "states"),
                "-config",
                str(root / "formal" / f"{config}.cfg"),
                str(root / "formal" / f"{model}.tla"),
            ]
            # File output avoids retaining a runaway TLC log in Python memory.
            with (output_dir / f"{config}.log").open("w+") as log:
                try:
                    completed = subprocess.run(
                        command,
                        cwd=temporary,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=timeout,
                    )
                    log.seek(0)
                    output = log.read(2_000_001)
                    result = classify(completed.returncode, output, invariant)
                    if len(output) > 2_000_000 or manifest["version"] not in output:
                        result["status"] = "FAIL"
                except subprocess.TimeoutExpired:
                    result = {"status": "FAIL", "reason": "TIMEOUT"}
        results.append({"case": config, **result})
    report = {
        "schema_version": 1,
        "scope": "finite safety models; no liveness or whole-system proof",
        "tool": manifest,
        "java_version": (version.stdout + version.stderr).strip(),
        "inputs": inputs,
        "cases": results,
        "status": "PASS" if all(r["status"] == "PASS" for r in results) else "FAIL",
    }
    (output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
