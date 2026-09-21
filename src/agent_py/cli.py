import asyncio
import json
from contextvars import ContextVar
from pathlib import Path

import typer
from sqlalchemy import select

from agent_py.config import get_settings
from agent_py.db import Approval, Grant, Policy
from agent_py.domain import Principal, TaskContract
from agent_py.harness import SimulationHarness
from agent_py.runtime import build_service as runtime_build_service
from agent_py.runtime import serve_dispatcher, serve_worker
from agent_py.security import issue_dev_token

app = typer.Typer(no_args_is_help=True)
_cli_context = ContextVar("agent_cli_context", default=None)


@app.callback()
def command_context(ctx: typer.Context):
    token = _cli_context.set(ctx)
    ctx.call_on_close(lambda: _cli_context.reset(token))


def build_service(settings):
    service = runtime_build_service(settings)
    if context := _cli_context.get():
        context.call_on_close(service.telemetry.close)
    return service


def principal(tenant="demo", subject="developer"):
    return Principal(
        tenant_id=tenant,
        subject=subject,
        roles=["developer", "operator"] if subject == "developer" else ["approver", "operator"],
        projects=["demo"],
        environments=["lab"],
    )


@app.command()
def init():
    """Initialize development-only schema and the two demo principals."""
    settings = get_settings()
    if settings.environment == "production":
        raise typer.BadParameter("Use Alembic and explicit grants in production")
    service = build_service(settings)
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    with service.db.session("demo") as s:
        for subject in ["developer", "reviewer"]:
            if not s.scalar(
                select(Grant).where(Grant.tenant_id == "demo", Grant.subject == subject)
            ):
                s.add(Grant(tenant_id="demo", subject=subject, project="demo", environment="lab"))
    typer.echo("Initialized simulation database and demo grants. No external systems contacted.")


@app.command()
def token(subject: str = "developer"):
    """Issue a short-lived local token. This is an operator-only development command."""
    if subject not in {"developer", "reviewer"}:
        raise typer.BadParameter("Use developer or reviewer")
    typer.echo(issue_dev_token(get_settings(), principal(subject=subject)))


@app.command()
def auth_keys_check(file: Path):
    """Validate a local RSA JWKS and show public-key fingerprints without loading Service."""
    import hashlib

    from cryptography.hazmat.primitives import serialization

    from agent_py.auth_keys import load_keyset
    from agent_py.domain import DomainError

    try:
        keys = load_keyset(file)
    except DomainError as exc:
        typer.echo(f"{exc.code}: {exc.message}", err=True)
        raise typer.Exit(1) from None
    typer.echo(
        json.dumps(
            {
                "keys": [
                    {
                        "kid": kid,
                        "algorithm": "RS256",
                        "bits": key.key_size,
                        "spki_sha256": hashlib.sha256(
                            key.public_bytes(
                                serialization.Encoding.DER,
                                serialization.PublicFormat.SubjectPublicKeyInfo,
                            )
                        ).hexdigest(),
                    }
                    for kid, key in sorted(keys.items())
                ]
            },
            indent=2,
        )
    )


@app.command()
def api(host: str = "127.0.0.1", port: int = 8000):
    import uvicorn

    uvicorn.run("agent_py.api:create_app", factory=True, host=host, port=port)


@app.command()
def worker():
    asyncio.run(serve_worker(get_settings()))


@app.command()
def dispatcher(tenant: list[str] = typer.Option(...)):
    """Dispatch outbox and reconcile only explicitly provisioned tenants."""
    asyncio.run(serve_dispatcher(get_settings(), tenant))


@app.command()
def demo(kind: str = "repair", auto_approve_simulation: bool = False):
    """Run a deterministic local simulation; no model or enterprise calls."""
    from agent_py.db import uid

    if kind not in {"repair", "incident"}:
        raise typer.BadParameter("Use repair or incident")
    service = build_service(get_settings())
    if service.settings.execution_mode != "simulation":
        raise typer.BadParameter("Demo requires explicit simulation mode")
    init()
    t = service.create_task(
        principal(),
        TaskContract(
            kind=kind, goal=f"Investigate demo {kind}", project="demo", resource="demo-" + uid()
        ),
        uid(),
    )
    harness = SimulationHarness(service)
    for _ in range(30):
        outcome = harness.tick("demo", t.id)
        if outcome.get("wait") == "APPROVAL" and auto_approve_simulation:
            with service.db.session("demo") as s:
                a = s.get(Approval, outcome["approval_id"])
            service.decide(principal(subject="reviewer"), a.id, "approve", a.payload_digest)
        elif outcome.get("wait") or outcome.get("done"):
            typer.echo(json.dumps({"task_id": t.id, **outcome}, indent=2))
            return
    raise RuntimeError("Simulation did not converge within 30 ticks")


@app.command()
def tick(task_id: str, tenant: str = "demo"):
    from agent_py.investigation import build_harness

    service = build_service(get_settings())
    typer.echo(json.dumps(build_harness(service).tick(tenant, task_id), indent=2))


@app.command()
def emergency_stop(tenant: str, stopped: bool = True):
    service = build_service(get_settings())
    with service.db.session(tenant) as s:
        policy = s.scalar(select(Policy).where(Policy.tenant_id == tenant))
        if not policy:
            policy = Policy(tenant_id=tenant)
            s.add(policy)
        policy.stopped = stopped
    typer.echo("Dispatch policy updated. Reconciliation remains enabled.")


@app.command()
def admission_limit(tenant: str, limit: int):
    """Operator command: set shared concurrency policy without killing current work."""
    from agent_py.db import AdmissionGate
    from agent_py.scheduling import lock_tenant

    if not 1 <= limit <= 128:
        raise typer.BadParameter("Limit must be between 1 and 128")
    service = build_service(get_settings())
    with service.db.session(tenant) as s:
        lock_tenant(s, tenant)
        s.scalar(select(AdmissionGate).where(AdmissionGate.tenant_id == tenant)).max_active = limit
    typer.echo("Shared tenant admission limit updated; running leases remain valid.")


@app.command()
def dependency_reset(tenant: str, dependency: str):
    """Local operator override after checking provider health or correcting rate-limit policy."""
    from agent_py.resilience import reset_circuit

    reset_circuit(build_service(get_settings()), tenant, dependency)
    typer.echo("Circuit reset; previous permits cannot change the new generation.")


@app.command()
def dependency_status(tenant: str):
    """Local operator view of shared circuit state and active read slots; no credentials."""
    from agent_py.resilience import dependency_status as snapshot

    typer.echo(json.dumps(snapshot(build_service(get_settings()), tenant), indent=2))


@app.command()
def dependency_limit(tenant: str, dependency: str, limit: int):
    """Update a shared dependency limit without releasing or cancelling in-flight reads."""
    from agent_py.resilience import set_read_limit

    set_read_limit(build_service(get_settings()), tenant, dependency, limit)
    typer.echo("Shared read limit updated; existing leases remain counted until release or expiry.")


@app.command()
def ops_status():
    """Show grant-scoped operational state for the local demo operator."""
    from agent_py.operations import summary

    service = build_service(get_settings())
    typer.echo(json.dumps(summary(service, "demo", principal()), indent=2))


@app.command()
def export(task_id: str, output: Path, signed: bool = False):
    from agent_py.audit import export_audit

    service = build_service(get_settings())
    recording = export_audit(service, principal(), task_id)
    if signed:
        from agent_py.audit_signing import sign_recording

        if service.settings.audit_signing_manifest is None:
            raise typer.BadParameter("Configure AGENT_AUDIT_SIGNING_MANIFEST for signed export")
        recording = sign_recording(recording, service.settings.audit_signing_manifest)
        service.get_task(principal(), task_id)
    output.write_text(json.dumps(recording, indent=2))
    typer.echo(
        "Exported tenant-scoped recording. Review business-sensitive content before sharing."
    )


@app.command()
def audit_keygen(
    directory: Path, key_id: str, tenant: str, audience: str = "agent-audit", days: int = 90
):
    """Create owner-only signing files in a new directory; never overwrite existing keys."""
    from agent_py.audit_signing import generate_key_files

    try:
        paths = generate_key_files(directory, key_id, tenant, audience, days)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(
            "Cannot create key files; check scope, lifetime and use an empty directory"
        ) from exc
    typer.echo(json.dumps(paths, indent=2))


@app.command()
def audit_check(
    recording: Path,
    trust_store: Path | None = None,
    tenant: str | None = None,
    audience: str | None = None,
):
    """Check a v2 audit package offline; no production service or credentials are loaded."""
    from agent_py.audit import check_recording
    from agent_py.audit_signing import MAX_SIGNED_BYTES, decode_json, verify_recording
    from agent_py.domain import DomainError

    with recording.open("rb") as stream:
        raw = stream.read(MAX_SIGNED_BYTES + 1)
    if len(raw) > MAX_SIGNED_BYTES:
        raise typer.BadParameter("Recording exceeds signed-package limit")
    try:
        package = decode_json(raw)
        if isinstance(package, dict) and package.get("schema") == "signed-recording-v1":
            if trust_store is None or not tenant or not audience:
                raise typer.BadParameter(
                    "Signed packages require --trust-store, --tenant and --audience"
                )
            report = verify_recording(package, trust_store, tenant, audience)
        else:
            if trust_store is not None or tenant is not None or audience is not None:
                raise typer.BadParameter("Expected a signed package; refusing unsigned downgrade")
            report = {**check_recording(package), "signature_verified": False}
    except DomainError as exc:
        typer.echo(f"{exc.code}: {exc.message}", err=True)
        raise typer.Exit(1) from exc
    except (ValueError, RecursionError) as exc:
        raise typer.BadParameter("Invalid recording JSON") from exc
    typer.echo(json.dumps(report, indent=2))


@app.command()
def evaluate(split: str = "development", output: Path = Path(".runtime/evaluation.json")):
    """Run fixture conformance, not a model-quality benchmark."""
    from agent_py.evaluation import evaluate as run

    report = run(build_service(get_settings()), split, output)
    typer.echo(f"Simulation conformance: {report['passed']}/{report['total']}; {output}")
    if report["passed"] != report["total"]:
        raise typer.Exit(1)


@app.command()
def evaluation_gate(
    policy: Path,
    simulation: Path,
    baseline: Path,
    candidate: Path,
    fixture: Path,
    output: Path = Path(".runtime/evaluation-gate.json"),
):
    """Offline gate: exit 0 PASS, 1 FAIL, 2 INSUFFICIENT_EVIDENCE or invalid input paths."""
    from agent_py.evaluation_gate import run_gate

    try:
        report = run_gate(policy, simulation, baseline, candidate, fixture, output)
    except (OSError, ValueError, RecursionError):
        typer.echo(
            "Cannot write gate report; check local paths and input/output separation", err=True
        )
        raise typer.Exit(2) from None
    status = report["body"]["status"]
    typer.echo(f"Local evaluation gate: {status}; {output}; not production authorization")
    raise typer.Exit({"PASS": 0, "FAIL": 1, "INSUFFICIENT_EVIDENCE": 2}[status])


@app.command()
def ingest(task_id: str, file: Path):
    """Import an explicitly selected text document as untrusted, task-project evidence."""
    from agent_py.db import Document, now

    service = build_service(get_settings())
    p = principal()
    t = service.get_task(p, task_id)
    body = file.read_text()
    if len(body.encode()) > 200_000:
        raise typer.BadParameter("Document exceeds 200 KB")
    from agent_py.domain import digest

    with service.db.session(p.tenant_id) as s:
        d = Document(
            tenant_id=p.tenant_id,
            task_id=task_id,
            project=t.contract["project"],
            source="upload://" + file.name,
            version=digest(body),
            body=body,
            allowed_subjects=[p.subject],
            valid_from=now(),
        )
        s.add(d)
        s.flush()
        typer.echo(d.id)


@app.command()
def evaluate_retrieval(dataset: Path, output: Path = Path(".runtime/retrieval-evaluation.json")):
    """Compare retrieval strategies in a disposable database, without model or network calls."""
    from agent_py.retrieval_evaluation import evaluate_retrieval as run

    report = run(dataset, output)
    for result in report["results"]:
        typer.echo(
            f"{result['strategy']}: recall={result['mean_recall']:.3f}, MRR={result['mrr']:.3f}"
        )
    typer.echo(f"Authored retrieval fixtures only; report: {output}")


@app.command()
def context_preview(task_id: str, query: str | None = None, budget: int = 6000):
    """Preview authorized evidence without calling a model or modifying a task."""
    from agent_py.context import ContextCompiler

    service = build_service(get_settings())
    p = principal()
    t = service.get_task(p, task_id)
    compiler = ContextCompiler(
        service.db, service.settings.context_strategy, telemetry=service.telemetry
    )
    bundle = compiler.compile(
        p,
        t.contract["project"],
        t.contract["environment"],
        query if query is not None else t.contract["goal"],
        budget=budget,
        task_id=task_id,
    )
    compiler.validate(p, t.contract["project"], t.contract["environment"], bundle, task_id=task_id)
    typer.echo(json.dumps(bundle.as_dict(), ensure_ascii=False, indent=2))


@app.command()
def collect(task_id: str):
    """Read scoped sources from the operator's configured manifest into evidence storage."""
    from agent_py.collection import CollectionManifest, EvidenceCollector

    service = build_service(get_settings())
    if service.settings.collection_manifest is None:
        raise typer.BadParameter("Set AGENT_COLLECTION_MANIFEST to an operator-owned JSON file")
    collector = EvidenceCollector(
        service, CollectionManifest.load(service.settings.collection_manifest)
    )
    typer.echo(json.dumps(collector.collect(principal(), task_id)))


@app.command()
def verify_patch(task_id: str, source: Path, patch_file: Path):
    """Apply a proposed patch to disposable snapshots and run the configured container oracle."""
    from pydantic import TypeAdapter

    from agent_py.patches import FileEdit
    from agent_py.verification import VerificationRunner

    with patch_file.open("rb") as stream:
        raw = stream.read(2_000_001)
    if len(raw) > 2_000_000:
        raise typer.BadParameter("Patch JSON exceeds 2 MB")
    edits = TypeAdapter(list[FileEdit]).validate_json(raw)
    result = VerificationRunner(build_service(get_settings())).run(
        principal(), task_id, source, edits
    )
    typer.echo(json.dumps(result))
    if result["outcome"] != "REGRESSION_FIXED":
        raise typer.Exit(1)


@app.command()
def investigation_create(
    goal: str,
    kind: str = "incident",
    resource: str = "demo-service",
    request_key: str = typer.Option(...),
):
    """Create a bounded read-only investigation using the local demo operator identity."""
    if kind not in {"repair", "incident"}:
        raise typer.BadParameter("Use repair or incident")
    service = build_service(get_settings())
    task = service.create_task(
        principal(),
        TaskContract(
            kind=kind,
            workflow="investigation_loop",
            goal=goal,
            project="demo",
            resource=resource,
        ),
        request_key,
    )
    typer.echo(task.id)


@app.command()
def investigation_status(task_id: str):
    """Inspect frozen rounds without dispatching inference or collecting evidence."""
    from agent_py.investigation_status import investigation_details

    typer.echo(
        json.dumps(
            investigation_details(build_service(get_settings()), principal(), task_id),
            indent=2,
            ensure_ascii=False,
        )
    )


@app.command()
def repair_create(goal: str, resource: str = "demo-service", request_key: str = typer.Option(...)):
    """Create a bounded live candidate-repair task; configured Worker advances it."""
    service = build_service(get_settings())
    task = service.create_task(
        principal(),
        TaskContract(
            kind="repair", workflow="repair_candidate", goal=goal, project="demo", resource=resource
        ),
        request_key,
    )
    typer.echo(task.id)


@app.command()
def repair_status(task_id: str):
    from agent_py.repair import repair_details

    typer.echo(
        json.dumps(repair_details(build_service(get_settings()), principal(), task_id), indent=2)
    )


@app.command()
def analyze(
    task_id: str, input_micro_per_token: int, output_micro_per_token: int, allow_api: bool = False
):
    """Explicitly paid, read-only model analysis; never dispatch its proposed actions."""
    if not allow_api:
        raise typer.BadParameter("Pass --allow-api with explicit prices to permit a billed request")
    from agent_py.artifacts import ArtifactStore
    from agent_py.context import ContextCompiler
    from agent_py.db import uid
    from agent_py.model import AnthropicGateway

    service = build_service(get_settings())
    p = principal()
    t = service.get_task(p, task_id)
    compiler = ContextCompiler(
        service.db, service.settings.context_strategy, telemetry=service.telemetry
    )
    bundle = compiler.compile(
        p, t.contract["project"], t.contract["environment"], t.contract["goal"], task_id=task_id
    )
    if not bundle.documents:
        raise typer.BadParameter("Import authorized evidence before requesting model analysis")
    compiler.validate(p, t.contract["project"], t.contract["environment"], bundle, task_id=task_id)
    decision = AnthropicGateway(
        service.settings, service, input_micro_per_token, output_micro_per_token
    ).decide(p.tenant_id, t.id, uid(), t.contract["goal"], bundle.as_dict())
    compiler.validate(p, t.contract["project"], t.contract["environment"], bundle, task_id=task_id)
    data = {
        "context": bundle.as_dict(),
        "decision": decision.model_dump(),
        "executed": False,
        "model": service.settings.model_id,
    }
    a = ArtifactStore(service.db, service.settings.artifact_root).put(
        p.tenant_id, t.id, "model-analysis", json.dumps(data).encode()
    )
    typer.echo(f"Saved analysis artifact {a.id}; no proposed action was executed")


@app.command()
def release_build(
    output: Path,
    policy: Path = Path("examples/evaluation-gate-policy.json"),
    simulation: Path = Path(".runtime/gate/simulation.json"),
    baseline: Path = Path(".runtime/gate/retrieval.json"),
    candidate: Path = Path(".runtime/gate/retrieval.json"),
    fixture: Path = Path("examples/retrieval-development.json"),
    root: Path = Path("."),
):
    """Bind local source/configuration to recomputed evidence; does not authorize deployment."""
    from agent_py.domain import DomainError
    from agent_py.releases import (
        GateEvidence,
        create_release,
        decode_json,
        read_regular,
        release_id,
        write_release,
    )

    try:
        evidence = GateEvidence.model_validate(
            {
                name: decode_json(read_regular(path))
                for name, path in {
                    "policy": policy,
                    "simulation": simulation,
                    "baseline": baseline,
                    "candidate": candidate,
                    "fixture": fixture,
                }.items()
            }
        )
        release = create_release(get_settings(), root.resolve(), evidence)
        write_release(release, output)
    except (DomainError, OSError, ValueError, TypeError, RecursionError):
        typer.echo(
            "Release build failed: check evidence, runtime settings and output path", err=True
        )
        raise typer.Exit(1) from None
    typer.echo(json.dumps({"release_id": release_id(release), "production_ready": False}))


@app.command()
def release_check(manifest: Path, expected_id: str, root: Path = Path(".")):
    """Check a release against this process's checkout and settings, without Service or network."""
    from agent_py.domain import DomainError
    from agent_py.releases import ReleaseGuard

    settings = get_settings().model_copy(
        update={
            "release_manifest": manifest,
            "release_expected_id": expected_id,
            "release_root": root,
        }
    )
    try:
        guard = ReleaseGuard(settings)
    except DomainError as exc:
        typer.echo(f"{exc.code}: {exc.message}", err=True)
        raise typer.Exit(1) from None
    typer.echo(
        json.dumps(
            {
                "release_id": guard.id,
                "task_queue": guard.task_queue,
                "signature_required": guard.signature_required,
                "production_ready": False,
            }
        )
    )


@app.command()
def release_keygen(
    directory: Path, key_id: str, audience: str, environment: str = "test", days: int = 90
):
    """Generate operator-owned release signing material; distribute public trust separately."""
    from agent_py.release_signing import generate_release_keys

    try:
        result = generate_release_keys(directory, key_id, audience, environment, days)
    except (OSError, ValueError, TypeError):
        typer.echo("Release key generation failed: invalid scope or existing output", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result))


@app.command()
def release_sign(
    manifest: Path,
    signer: Path,
    expected_id: str,
    output: Path,
    audience: str,
    environment: str = "test",
    lifetime: int = 3600,
):
    """Sign a bounded release attestation; neither publish nor deploy the release."""
    from agent_py.domain import DomainError
    from agent_py.release_signing import local_json, sign_release, write_private_json
    from agent_py.releases import MAX_FILE, AgentRelease, release_id

    try:
        release = AgentRelease.model_validate(local_json(manifest, MAX_FILE))
        if release_id(release) != expected_id:
            raise ValueError("Independent release ID mismatch")
        signed = sign_release(release, signer, audience, environment, lifetime)
        write_private_json(output, signed)
    except (DomainError, OSError, ValueError, TypeError, RecursionError):
        typer.echo("Release signing failed: release, scope, key or output denied", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps({"release_id": expected_id, "production_ready": False}))


@app.command()
def release_verify(
    attestation: Path, trust: Path, expected_id: str, audience: str, environment: str = "test"
):
    """Verify only the detached signature against independent scope/pin; use release-check too."""
    from agent_py.domain import DomainError
    from agent_py.release_signing import verify_release_attestation

    try:
        result = verify_release_attestation(attestation, trust, expected_id, audience, environment)
    except DomainError as exc:
        typer.echo(f"{exc.code}: {exc.message}", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result))
