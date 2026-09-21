"""Three real Harness paths, synthetic model/sandbox, real loopback OTLP; no external calls."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ("investigate", "investigation_loop", "repair_candidate")
MODES = ("disabled", "healthy", "rejected")
PREFIX = "langfuse.observation."
WORKFLOW_FIELDS = (
    "workflow",
    "round",
    "repair_run_id",
    "candidate_id",
    "candidate_ordinal",
    "verification_id",
    "verification_outcome",
    "waiting_reason",
    "phase",
    "stop_reason",
    "rounds",
    "changed",
    "baseline_digest",
    "candidate_digest",
    "oracle_digest",
)
PRIVATE = "PRIVATE_TRAJECTORY_CANARY"
EXPECTED = {"investigate": (1, 50), "investigation_loop": (3, 150), "repair_candidate": (1, 40)}


def inputs():
    paths = [*ROOT.glob("src/agent_py/**/*.py"), Path(__file__).resolve(), ROOT / "uv.lock"]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def validate_case(case):
    """Recompute acceptance from child evidence, never trust a child PASS label."""
    errors = []

    def require(condition, code):
        if not condition:
            errors.append(code)

    workflow, mode = case["workflow"], case["mode"]
    calls, spent = EXPECTED[workflow]
    business = case["business"]
    require(business["model_calls"] == calls, "model_call_count")
    require(business["spent"] == business["ledger_actual"] == spent, "ledger_cost")
    require(business["reserved"] == 0 and business["reservations"] == calls, "ledger_settlement")
    require(business["operations"] == 0, "unexpected_action")
    require(business["status"] == "WAITING" and business["result"] is None, "business_completion")
    require(business["repeat_stable"] is True, "recovery_reuse")
    require(business["service_recreated"] is True, "recovery_not_exercised")
    require(business["source_unchanged"] is True, "source_mutation")
    require(
        business["sandbox_calls"] == (2 if workflow == "repair_candidate" else 0), "sandbox_count"
    )
    require(
        business["stop_reason"]
        == {
            "investigate": "HUMAN_REVIEW",
            "investigation_loop": "ROUND_LIMIT",
            "repair_candidate": "REGRESSION_FIXED",
        }[workflow],
        "stop_reason",
    )
    require(case["privacy_clean"] is True, "privacy")
    spans, counters = case["spans"], case["counters"]
    if mode == "disabled":
        require(not spans and not counters and case["http_requests"] == 0, "disabled_export")
        return errors
    require(bool(spans), "missing_spans")
    require(counters.get("queued", 0) == len(spans), "queue_count")
    key = "exported" if mode == "healthy" else "export_failed"
    require(counters.get(key, 0) == len(spans), "delivery_count")
    require(
        counters.get("queued", 0) == counters.get("exported", 0) + counters.get("export_failed", 0),
        "delivery_accounting",
    )
    require(
        not any(
            counters.get(k, 0) for k in ("dropped", "sanitization_failed", "correlation_failed")
        ),
        "export_loss",
    )
    names = Counter(s["name"] for s in spans)
    require(names["model.generation"] == calls, "generation_count")
    require(names["task.accepted"] == 1 and names["worker.tick"] >= 2, "roots_missing")
    require(names["retrieval.compile"] >= 1, "retrieval_missing")
    require(names["sandbox.verify"] == business["sandbox_calls"], "sandbox_missing")
    if workflow != "repair_candidate":
        require(names["model.result_reused"] >= calls, "reuse_observation_missing")
    identities = {(s["trace"], s["id"]): s for s in spans}
    require(len(identities) == len(spans), "duplicate_span")
    sessions = {s["session"] for s in spans}
    require(len(sessions) == 1 and all(len(s) == 64 for s in sessions), "session_identity")
    ticks = [s for s in spans if s["name"] == "worker.tick"]
    for previous, current in zip(ticks, ticks[1:]):
        require(
            any(
                link["role"] == "previous"
                and (link["trace"], link["id"]) == (previous["trace"], previous["id"])
                for link in current["links"]
            ),
            "previous_tick_missing",
        )
    generations, total = {}, Decimal(0)
    for span in spans:
        if span["name"] not in {"task.accepted", "worker.tick"}:
            parent = identities.get((span["trace"], span["parent"]))
            require(parent is not None and parent["name"] == "worker.tick", "parent_missing")
        if span["name"] == "worker.tick":
            require(not span["parent"], "tick_not_root")
            require(any(link["role"] == "origin" for link in span["links"]), "origin_missing")
            for link in span["links"]:
                target = identities.get((link["trace"], link["id"]))
                require(
                    target is not None
                    and target["name"]
                    == {"origin": "task.accepted", "previous": "worker.tick"}.get(link["role"]),
                    "link_target_missing",
                )
        if span["name"] == "model.generation":
            require(span["inference"] not in generations, "duplicate_inference")
            generations[span["inference"]] = span
            require(
                span["usage"]
                == {"input": 20 if workflow == "repair_candidate" else 30, "output": 10},
                "usage_mismatch",
            )
            require(span["outcome"] == "validated", "generation_outcome")
            require(
                len(span["request_digest"]) == 64 and len(span["context_digest"]) == 64,
                "digest_missing",
            )
            require(set(span["cost"]) == {"input", "output"}, "cost_missing")
            require(
                span["cost"]
                == {
                    "input": (20 if workflow == "repair_candidate" else 30) / 1_000_000,
                    "output": 20 / 1_000_000,
                },
                "generation_cost",
            )
            total += sum((Decimal(str(v)) for v in span["cost"].values()), Decimal(0))
        elif span["name"] == "model.result_reused":
            require(not span["usage"] and not span["cost"], "reuse_double_count")
    for span in spans:
        if span["name"] == "model.result_reused":
            original = generations.get(span["inference"])
            require(
                original is not None and original["request_digest"] == span["request_digest"],
                "reuse_identity",
            )
            if workflow == "investigation_loop":
                require(
                    original is not None
                    and original["workflow_metadata"].get("round")
                    == span["workflow_metadata"].get("round"),
                    "reuse_round",
                )
    require(
        ticks
        and ticks[-1]["workflow_metadata"].get("waiting_reason")
        == ("CANDIDATE_READY_FOR_REVIEW" if workflow == "repair_candidate" else "HUMAN_REVIEW"),
        "worker_waiting_reason",
    )
    if workflow == "investigation_loop":
        require(
            sorted(s["workflow_metadata"].get("round", 0) for s in generations.values())
            == [1, 2, 3],
            "round_identity",
        )
    if workflow != "repair_candidate":
        summaries = [s["workflow_metadata"] for s in spans if s["name"] == "investigation.summary"]
        require(
            len(summaries) == 2 and [s.get("changed") for s in summaries] == [True, False],
            "summary_reuse",
        )
        require(
            all(
                s.get("stop_reason") == business["stop_reason"] and s.get("rounds") == calls
                for s in summaries
            ),
            "summary_reason",
        )
    else:
        candidates = [
            s["workflow_metadata"]
            for s in spans
            if s["name"] in {"model.generation", "sandbox.verify", "verification.result"}
        ]
        require(
            len(candidates) == 4
            and len({s.get("candidate_id") for s in candidates}) == 1
            and all(
                len(s.get("candidate_id", "")) == 64 and s.get("candidate_ordinal") == 1
                for s in candidates
            ),
            "candidate_identity",
        )
        require(
            len({s.get("repair_run_id") for s in candidates}) == 1
            and all(len(s.get("repair_run_id", "")) == 64 for s in candidates),
            "repair_run_identity",
        )
        verifications = [
            s["workflow_metadata"]
            for s in spans
            if s["name"] in {"sandbox.verify", "verification.result"}
        ]
        require(
            len({s.get("verification_id") for s in verifications}) == 1
            and all(len(s.get("verification_id", "")) == 64 for s in verifications),
            "verification_identity",
        )
        results = [s["workflow_metadata"] for s in spans if s["name"] == "verification.result"]
        require(
            len(results) == 1 and results[0].get("verification_outcome") == business["stop_reason"],
            "verification_result",
        )
        require(
            all(
                all(
                    len(s.get(key, "")) == 64
                    for key in ("baseline_digest", "candidate_digest", "oracle_digest")
                )
                for s in results
            ),
            "verification_digests",
        )
    require(total * 1_000_000 == spent, "export_cost")
    return sorted(set(errors))


def decode(payloads):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    records = []
    for raw in payloads:
        message = ExportTraceServiceRequest.FromString(raw)
        for resource in message.resource_spans:
            for scope in resource.scope_spans:
                for span in scope.spans:
                    attrs = {
                        a.key: getattr(a.value, a.value.WhichOneof("value"))
                        for a in span.attributes
                    }
                    records.append(
                        {
                            "name": span.name,
                            "trace": span.trace_id.hex(),
                            "id": span.span_id.hex(),
                            "parent": span.parent_span_id.hex(),
                            "session": attrs.get("session.id", ""),
                            "links": [
                                {
                                    "trace": link.trace_id.hex(),
                                    "id": link.span_id.hex(),
                                    "role": next(
                                        (
                                            a.value.string_value
                                            for a in link.attributes
                                            if a.key == "agent.link"
                                        ),
                                        "",
                                    ),
                                }
                                for link in span.links
                            ],
                            "inference": attrs.get(PREFIX + "metadata.inference_id", ""),
                            "request_digest": attrs.get(PREFIX + "metadata.request_digest", ""),
                            "context_digest": attrs.get(PREFIX + "metadata.context_digest", ""),
                            "outcome": attrs.get(PREFIX + "metadata.outcome", ""),
                            "workflow_metadata": {
                                key: attrs[PREFIX + "metadata." + key]
                                for key in WORKFLOW_FIELDS
                                if PREFIX + "metadata." + key in attrs
                            },
                            "usage": json.loads(attrs.get(PREFIX + "usage_details", "{}")),
                            "cost": json.loads(attrs.get(PREFIX + "cost_details", "{}")),
                        }
                    )
    return records


def run_case(workflow, mode, directory):
    import httpx
    from sqlalchemy import func, select

    from agent_py.adapters.simulation import DisabledLiveExecutor
    from agent_py.artifacts import ArtifactStore
    from agent_py.config import Settings
    from agent_py.db import Database, Document, Grant, Operation, Reservation
    from agent_py.domain import Principal, TaskContract
    from agent_py.investigation import InvestigationHarness
    from agent_py.model import AnthropicGateway
    from agent_py.repair import RepairHarness, repair_details
    from agent_py.runtime import Activities
    from agent_py.sandbox import SandboxResult
    from agent_py.service import Service
    from agent_py.verification import VerificationRunner

    payloads, model_calls, sandbox_calls = [], [], []

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/api/public/otel/v1/traces" or not 0 < length <= 1048576:
                self.send_error(400)
                return
            payloads.append(self.rfile.read(length))
            self.send_response(503 if mode == "rejected" else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service, db = None, None
    try:
        source = directory / "repo"
        (source / "src").mkdir(parents=True)
        original = f"# {PRIVATE}\ndef value():\n    return 1\n"
        (source / "src/app.py").write_text(original)
        oracle = directory / "oracle"
        oracle.mkdir()
        (oracle / "test_app.py").write_text("def test_fixture(): pass\n")
        manifest = directory / "repair.json"
        manifest.write_text(
            json.dumps(
                {
                    "repositories": [
                        {
                            "tenant": "probe",
                            "project": "demo",
                            "environment": "lab",
                            "resource": "demo-service",
                            "subjects": ["actor"],
                            "source_root": str(source),
                            "allow_model_export": True,
                            "max_attempts": 2,
                        }
                    ]
                }
            )
        )
        settings = Settings(
            _env_file=None,
            environment="test",
            execution_mode="live",
            database_url=f"sqlite:///{directory / 'app.db'}",
            artifact_root=directory / "artifacts",
            allow_model_api=True,
            model_id="synthetic-model",
            model_api_key=PRIVATE + "_MODEL_KEY",
            model_input_micro_per_token=1,
            model_output_micro_per_token=2,
            allow_candidate_execution=True,
            repair_manifest=manifest,
            sandbox_oracle=oracle,
            sandbox_root=directory / "sandbox",
            sandbox_image="python@sha256:" + "a" * 64,
            langfuse_enabled=mode != "disabled",
            langfuse_base_url=f"http://127.0.0.1:{server.server_port}",
            langfuse_tenant="probe",
            langfuse_public_key="synthetic-public",
            langfuse_secret_key=PRIVATE + "_EXPORT_KEY",
            langfuse_pseudonym_key=PRIVATE * 3,
            langfuse_sample_rate=1,
            langfuse_queue_size=4096,
            langfuse_timeout_seconds=1,
            langfuse_flush_seconds=3,
        )
        db = Database(settings.database_url)
        db.create_schema()
        service = Service(db, settings, DisabledLiveExecutor())
        principal = Principal(
            tenant_id="probe",
            subject="actor",
            roles=["developer", "operator"],
            projects=["demo"],
            environments=["lab"],
        )
        with db.session("probe") as s:
            s.add(Grant(tenant_id="probe", subject="actor", project="demo", environment="lab"))
            for word in ("alpha", "beta", "gamma"):
                s.add(
                    Document(
                        id=word,
                        tenant_id="probe",
                        project="demo",
                        source="fixture://" + PRIVATE,
                        version="1",
                        body=word + " " + PRIVATE,
                        allowed_subjects=["actor"],
                    )
                )
        task = service.create_task(
            principal,
            TaskContract(
                kind="repair" if workflow == "repair_candidate" else "incident",
                workflow=workflow,
                goal="Investigate alpha " + PRIVATE,
                project="demo",
            ),
            "synthetic-request",
        )

        def model(request):
            model_calls.append(1)
            context = json.loads(json.loads(request.content)["messages"][0]["content"])["context"]
            if workflow == "repair_candidate":
                body = context["sources"]["src/app.py"]
                name, decision = (
                    "submit_patch",
                    {
                        "summary": PRIVATE,
                        "evidence_ids": ["source:src/app.py"],
                        "edits": [
                            {
                                "path": "src/app.py",
                                "original_sha256": hashlib.sha256(body.encode()).hexdigest(),
                                "content": body.replace("return 1", "return 2"),
                            }
                        ],
                    },
                )
            else:
                name = (
                    "submit_investigation"
                    if workflow == "investigation_loop"
                    else "submit_decision"
                )
                decision = {
                    "summary": PRIVATE,
                    "hypotheses": [PRIVATE],
                    "evidence_ids": [context["documents"][0]["id"]],
                    "stop": workflow == "investigate",
                }
                if workflow == "investigation_loop":
                    decision["next_query"] = ("beta", "gamma", "delta")[context["round"] - 1]
            return httpx.Response(
                200,
                json={
                    "stop_reason": "tool_use",
                    "usage": {
                        "input_tokens": 20 if workflow == "repair_candidate" else 30,
                        "output_tokens": 10,
                    },
                    "content": [{"type": "tool_use", "name": name, "input": decision}],
                },
            )

        class Sandbox:
            def __init__(self, *args):
                pass

            def verify(self, workspace, **kwargs):
                sandbox_calls.append(1)
                return SandboxResult(
                    0 if "return 2" in (workspace / "src/app.py").read_text() else 1, PRIVATE
                )

        def build_activities(current_service):
            gateway = AnthropicGateway(settings, current_service, 1, 2, httpx.MockTransport(model))
            activities = Activities(current_service)
            activities.harness = (
                RepairHarness(
                    current_service, gateway, VerificationRunner(current_service, Sandbox)
                )
                if workflow == "repair_candidate"
                else InvestigationHarness(current_service, gateway)
            )
            return activities

        def export_counts(current_service):
            return Counter(
                {
                    sample.labels["result"]: int(sample.value)
                    for metric in current_service.telemetry.langfuse_events.collect()
                    for sample in metric.samples
                    if sample.name.endswith("_total")
                }
            )

        activities = build_activities(service)
        identity = {"tenant": "probe", "task_id": task.id}
        for _ in range(8):
            result = asyncio.run(activities.tick(identity))
            if result.get("wait") in {"HUMAN_REVIEW", "CANDIDATE_READY_FOR_REVIEW"}:
                break
        service.telemetry.close()
        counters = export_counts(service)
        # Recreate providers, Harness and DB connection pool; only persisted state survives.
        db.engine.dispose()
        db = Database(settings.database_url)
        service = Service(db, settings, DisabledLiveExecutor())
        activities = build_activities(service)
        repeat = asyncio.run(activities.tick(identity))
        current = service.get_task(principal, task.id)
        stop_reason = result.get("wait")
        if workflow == "investigation_loop" and result.get("artifact_id"):
            stop_reason = json.loads(
                ArtifactStore(db, settings.artifact_root).read(principal, result["artifact_id"])[1]
            )["stop_reason"]
        if workflow == "repair_candidate":
            details = repair_details(service, principal, task.id)
            stop_reason = (
                details["attempts"][-1]["outcome"] if details and details["attempts"] else None
            )
        with db.session("probe") as s:
            reservations = s.scalars(select(Reservation)).all()
            business = dict(
                model_calls=len(model_calls),
                sandbox_calls=len(sandbox_calls),
                spent=current.spent,
                reserved=current.reserved,
                reservations=len(reservations),
                ledger_actual=sum(r.actual or 0 for r in reservations),
                operations=s.scalar(select(func.count()).select_from(Operation)),
                status=current.status,
                result=current.result,
                stop_reason=stop_reason,
                repeat_stable=result == repeat,
                service_recreated=True,
                source_unchanged=(source / "src/app.py").read_text() == original,
            )
        service.telemetry.close()
        counters.update(export_counts(service))
        wire = b"".join(payloads)
        return dict(
            workflow=workflow,
            mode=mode,
            business=business,
            counters=counters,
            spans=decode(payloads),
            http_requests=len(payloads),
            privacy_clean=all(v.encode() not in wire for v in (PRIVATE, task.id, str(directory))),
        )
    finally:
        if service:
            service.telemetry.close()
        if db:
            db.engine.dispose()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".runtime/langfuse-trajectories")
    parser.add_argument("--case", choices=WORKFLOWS, help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=MODES, default="healthy", help=argparse.SUPPRESS)
    args = parser.parse_args()
    for key in list(os.environ):
        if key.startswith(("AGENT_", "LANGFUSE_", "OTEL_", "ANTHROPIC_", "OPENAI_")):
            del os.environ[key]
    if args.case:
        with tempfile.TemporaryDirectory(prefix="agent-trajectory-") as temporary:
            result = run_case(args.case, args.mode, Path(temporary))
        print(json.dumps(result))
        return
    args.output_dir.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir))
    before, cases, errors = inputs(), [], []
    for workflow in WORKFLOWS:
        for mode in MODES:
            try:
                child = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--case",
                        workflow,
                        "--mode",
                        mode,
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                if child.returncode:
                    raise ValueError("child failed")
                case = json.loads(child.stdout)
                if case["workflow"] != workflow or case["mode"] != mode:
                    raise ValueError("child identity mismatch")
                failures = validate_case(case)
                errors.extend(f"{workflow}:{mode}:{failure}" for failure in failures)
                cases.append(case)
            except (
                subprocess.TimeoutExpired,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                InvalidOperation,
            ):
                errors.append(f"{workflow}:{mode}:missing_or_invalid_evidence")
    for workflow in WORKFLOWS:
        group = [c for c in cases if c["workflow"] == workflow]
        if len(group) != 3 or any(c["business"] != group[0]["business"] for c in group):
            errors.append(f"{workflow}:business_equivalence")
    if inputs() != before:
        errors.append("inputs_changed")
    report = dict(
        schema_version=2,
        scope="synthetic_model_sandbox_real_harness_loopback_otlp",
        correctness="FAIL" if errors else "PASS",
        platform="NOT_RUN",
        real_model="NOT_RUN",
        real_sandbox="NOT_RUN",
        performance="NOT_ASSESSED",
        cases=cases,
        errors=errors,
        inputs=before,
    )
    path = directory / "report.json"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    print(f"Trajectory check: {report['correctness']}; report: {path}")
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
