import asyncio
import json
from pathlib import Path

import typer
from sqlalchemy import select

from agent_py.config import get_settings
from agent_py.db import Approval, Grant, Policy
from agent_py.domain import Principal, TaskContract
from agent_py.harness import SimulationHarness
from agent_py.runtime import build_service, serve_dispatcher, serve_worker
from agent_py.security import issue_dev_token

app = typer.Typer(no_args_is_help=True)


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
def export(task_id: str, output: Path):
    from agent_py.replay import export_recording

    recording = export_recording(build_service(get_settings()), principal(), task_id)
    output.write_text(json.dumps(recording, indent=2))
    typer.echo(
        "Exported tenant-scoped recording. Review business-sensitive content before sharing."
    )


@app.command()
def evaluate(split: str = "development", output: Path = Path(".runtime/evaluation.json")):
    """Run fixture conformance, not a model-quality benchmark."""
    from agent_py.evaluation import evaluate as run

    report = run(build_service(get_settings()), split, output)
    typer.echo(f"Simulation conformance: {report['passed']}/{report['total']}; {output}")
    if report["passed"] != report["total"]:
        raise typer.Exit(1)


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
    compiler = ContextCompiler(service.db)
    bundle = compiler.compile(
        p, t.contract["project"], t.contract["environment"], t.contract["goal"], task_id=task_id
    )
    if not bundle.documents:
        raise typer.BadParameter("Import authorized evidence before requesting model analysis")
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
