"""Isolated loopback OTLP fault/performance probe. Never uses project credentials."""

import argparse
import hashlib
import json
import math
import os
import resource
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODES = ("disabled", "healthy", "sampled", "rejected", "blocked")


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def inputs():
    paths = [*(ROOT / "src/agent_py").rglob("*.py"), Path(__file__).resolve(), ROOT / "uv.lock"]
    return {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)
    }


def run_case(mode, count, directory):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from sqlalchemy import func, select

    from agent_py.adapters.simulation import SimulatedSystem
    from agent_py.config import Settings
    from agent_py.db import Database, Grant, TaskTrace
    from agent_py.domain import ActionProposal, Principal, TaskContract, digest
    from agent_py.observation_policy import selected
    from agent_py.service import Service

    release = threading.Event()
    payloads = []

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/api/public/otel/v1/traces" or not 0 < length <= 16384:
                self.send_error(400)
                return
            payloads.append(self.rfile.read(length))
            if mode == "blocked":
                release.wait(5)
            self.send_response(503 if mode == "rejected" else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service = None
    try:
        settings = Settings(
            environment="test",
            execution_mode="simulation",
            _env_file=None,
            database_url=f"sqlite:///{directory / 'app.db'}",
            artifact_root=directory / "artifacts",
            langfuse_enabled=mode != "disabled",
            langfuse_base_url=f"http://127.0.0.1:{server.server_port}",
            langfuse_tenant="probe",
            langfuse_public_key="probe-public",
            langfuse_secret_key="probe-secret",
            langfuse_pseudonym_key="probe-pseudonym-key-not-a-credential" * 2,
            langfuse_sample_rate=0.5 if mode == "sampled" else 1,
            langfuse_queue_size=4 if mode == "blocked" else 4096,
            langfuse_timeout_seconds=0.2,
            langfuse_flush_seconds=2,
        )
        db = Database(settings.database_url)
        db.create_schema()
        service = Service(db, settings, SimulatedSystem(directory / "authority.db"))
        with db.session("probe") as session:
            session.add(
                Grant(tenant_id="probe", subject="actor", project="demo", environment="lab")
            )
        principal = Principal(
            tenant_id="probe",
            subject="actor",
            roles=["developer"],
            projects=["demo"],
            environments=["lab"],
        )
        durations, tasks, failures = [], [], []
        chosen = 0
        cpu_start, wall_start = time.process_time(), time.perf_counter()
        for i in range(count):
            started = time.perf_counter()
            task = service.create_task(
                principal,
                TaskContract(kind="repair", project="demo", goal="PRIVATE_PROBE_GOAL"),
                f"probe-{i}",
            )
            op = service.propose(
                principal,
                task.id,
                "prepare",
                ActionProposal(
                    tool="create_pr",
                    resource="demo-service",
                    parameters={"candidate_sha": digest("candidate"), "base_sha": digest("base")},
                ),
            )
            result = service.execute("probe", op.id)
            durations.append((time.perf_counter() - started) * 1000)
            tasks.append(task.id)
            chosen += selected(settings, "probe", task.id)
            if result.status != "SUCCEEDED" or result.attempts != 1:
                failures.append("business_result")
        active_seconds = time.perf_counter() - wall_start
        cpu_seconds = time.process_time() - cpu_start
        release.set()
        close_started = time.perf_counter()
        service.telemetry.close()
        close_ms = (time.perf_counter() - close_started) * 1000
        counters = {
            sample.labels["result"]: int(sample.value)
            for metric in service.telemetry.langfuse_events.collect()
            for sample in metric.samples
            if sample.name.endswith("_total")
        }
        wire = b"".join(payloads)
        if (
            b"PRIVATE_PROBE" in wire
            or b"probe-secret" in wire
            or any(t.encode() in wire for t in tasks)
        ):
            failures.append("privacy")
        names = Counter()
        for raw in payloads:
            request = ExportTraceServiceRequest.FromString(raw)
            for resource_span in request.resource_spans:
                for scope in resource_span.scope_spans:
                    names.update(span.name for span in scope.spans)
        with db.session("probe") as session:
            trace_rows = session.scalar(select(func.count()).select_from(TaskTrace))
        if trace_rows != chosen:
            failures.append("sampling_persistence")
        if service.remote.snapshot("probe", "demo-service")["effect_count"] != count:
            failures.append("side_effect_count")
        if mode in {"healthy", "sampled"}:
            if counters.get("exported", 0) != chosen * 3 or sum(names.values()) != chosen * 3:
                failures.append("incomplete_export")
            if counters.get("sampled_out", 0) != (count - chosen) * 3:
                failures.append("sampling_count")
        if mode == "disabled" and (payloads or counters):
            failures.append("disabled_export")
        if mode == "rejected" and counters.get("export_failed", 0) != count * 3:
            failures.append("rejection_accounting")
        if mode == "blocked" and counters.get("dropped", 0) == 0:
            failures.append("backpressure_not_exercised")
        if counters.get("queued", 0) != counters.get("exported", 0) + counters.get(
            "export_failed", 0
        ):
            failures.append("undrained_queue")
        return dict(
            mode=mode,
            tasks=count,
            selected_tasks=chosen,
            failures=sorted(set(failures)),
            latency_ms=durations,
            p50_ms=percentile(durations, 0.5),
            p95_ms=percentile(durations, 0.95),
            active_seconds=active_seconds,
            tasks_per_second=count / active_seconds,
            cpu_seconds=cpu_seconds,
            peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            close_ms=close_ms,
            counters=counters,
            received_spans=dict(names),
        )
    finally:
        release.set()
        if service:
            service.telemetry.close()
            service.db.engine.dispose()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=40)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".runtime/langfuse")
    parser.add_argument("--max-added-p95-ms", type=float)
    parser.add_argument("--case", choices=MODES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 8 <= args.tasks <= 500 or not 1 <= args.repeats <= 10:
        parser.error("tasks must be 8..500; repeats must be 1..10")
    if args.max_added_p95_ms is not None and (
        not math.isfinite(args.max_added_p95_ms) or args.max_added_p95_ms < 0
    ):
        parser.error("max-added-p95-ms must be finite and nonnegative")
    # Even an explicitly invoked child cannot inherit real credentials/configuration.
    for key in list(os.environ):
        if key.startswith("AGENT_"):
            del os.environ[key]
    if args.case:
        with tempfile.TemporaryDirectory(prefix="langfuse-probe-") as temporary:
            result = run_case(args.case, args.tasks, Path(temporary))
        print(json.dumps(result))
        return
    args.output_dir.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir))
    original_inputs = inputs()
    results, errors = [], []
    for repeat in range(args.repeats):
        order = MODES[repeat % len(MODES) :] + MODES[: repeat % len(MODES)]
        for mode in order:
            try:
                child = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--case",
                        mode,
                        "--tasks",
                        str(args.tasks),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if child.returncode:
                    errors.append(f"{repeat}:{mode}:child_failed")
                    continue
                result = json.loads(child.stdout)
                result["repeat"] = repeat
                results.append(result)
                errors.extend(f"{repeat}:{mode}:{reason}" for reason in result["failures"])
            except (subprocess.TimeoutExpired, ValueError):
                errors.append(f"{repeat}:{mode}:child_missing_or_invalid")
    comparisons = []
    for repeat in range(args.repeats):
        cases = {r["mode"]: r for r in results if r["repeat"] == repeat}
        if len(cases) != len(MODES):
            errors.append(f"{repeat}:incomplete_cases")
            continue
        for mode in MODES[1:]:
            delta = cases[mode]["p95_ms"] - cases["disabled"]["p95_ms"]
            comparisons.append(dict(repeat=repeat, mode=mode, added_p95_ms=delta))
    performance = "NOT_ASSESSED"
    if args.max_added_p95_ms is not None:
        performance = (
            "PASS"
            if len(comparisons) == args.repeats * 4
            and all(c["added_p95_ms"] <= args.max_added_p95_ms for c in comparisons)
            else "FAIL"
        )
    if inputs() != original_inputs:
        errors.append("inputs_changed_during_probe")
    report = dict(
        schema_version=1,
        scope="local_simulation_loopback_http",
        cases=results,
        comparisons=comparisons,
        max_added_p95_ms=args.max_added_p95_ms,
        correctness="FAIL" if errors else "PASS",
        performance=performance,
        errors=errors,
        inputs=original_inputs,
        limitations=[
            "Not a Langfuse platform or real model acceptance test",
            "RSS includes interpreter/SDK startup; CPU covers active workload only",
            "No warmup exclusion or confidence intervals; exploratory local comparison",
        ],
    )
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"Langfuse local probe: {report['correctness']}; performance: {performance}; report: {directory / 'report.json'}"
    )
    raise SystemExit(1 if errors or performance == "FAIL" else 0)


if __name__ == "__main__":
    main()
