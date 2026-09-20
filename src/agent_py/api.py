import asyncio
import hmac
import json
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from agent_py.adapters.simulation import DisabledLiveExecutor, SimulatedSystem
from agent_py.artifacts import ArtifactStore
from agent_py.config import Settings, get_settings
from agent_py.db import Approval, Artifact, Database, Operation, Task, tenant_get
from agent_py.domain import ActionProposal, ApprovalDecision, DomainError, Principal, TaskContract
from agent_py.security import authenticate, authorize
from agent_py.service import Service


def task_json(t):
    return {
        "id": t.id,
        "contract": t.contract,
        "status": t.status,
        "result": t.result,
        "waiting_reason": t.waiting_reason,
        "cancelled": t.cancelled,
        "taken_over": t.taken_over,
        "version": t.version,
        "spent_micro_usd": t.spent,
        "reserved_micro_usd": t.reserved,
        "created_at": t.created_at.isoformat(),
    }


def operation_json(op):
    return {
        "id": op.id,
        "task_id": op.task_id,
        "tool": op.tool,
        "resource": op.resource,
        "parameters": op.parameters,
        "payload_digest": op.payload_digest,
        "status": op.status,
        "recovery_status": op.recovery_status,
        "attempts": op.attempts,
        "result": op.result,
        "error": op.error,
    }


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_key: str = Field(min_length=1, max_length=160)
    action: ActionProposal


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=1)


class VerificationRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(min_length=1, max_length=36)


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str | None = Field(default=None, min_length=1, max_length=8000)
    budget: int = Field(default=6000, ge=1, le=100_000)


def create_app(
    settings: Settings | None = None, db: Database | None = None, remote=None
) -> FastAPI:
    settings = settings or get_settings()
    db = db or Database(settings.database_url)
    if settings.environment == "production":
        db.assert_production_role()
    remote = remote or (
        SimulatedSystem(settings.artifact_root.parent / "authority.db")
        if settings.execution_mode == "simulation"
        else DisabledLiveExecutor()
    )
    service = Service(db, settings, remote)
    store = ArtifactStore(db, settings.artifact_root)

    @asynccontextmanager
    async def lifespan(app):
        yield
        service.telemetry.close()

    app = FastAPI(title="Agent Engineering Workbench", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    @app.middleware("http")
    async def telemetry(request: Request, call_next):
        started, status = time.monotonic(), 500
        with service.telemetry.span("http.request") as span:
            try:
                response = await call_next(request)
                status = response.status_code
                response.headers["X-Trace-ID"] = format(span.get_span_context().trace_id, "032x")
                return response
            finally:
                route = getattr(request.scope.get("route"), "path", "unmatched")
                method = (
                    request.method
                    if request.method
                    in {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"}
                    else "OTHER"
                )
                span.set_attribute("http.method", method)
                span.set_attribute("http.route", route)
                span.set_attribute("http.status_code", status)
                service.telemetry.http.labels(method, route, str(status)).observe(
                    time.monotonic() - started
                )

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status
        )

    def current(authorization: Annotated[str | None, Header()] = None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise DomainError("UNAUTHENTICATED", "Bearer token required", 401)
        return authenticate(settings, authorization[7:])

    Auth = Annotated[Principal, Depends(current)]

    @app.post("/api/v1/tasks/{task_id}/context/preview")
    def context_preview(task_id: str, body: ContextRequest, p: Auth):
        from agent_py.context import ContextCompiler

        task = service.get_task(p, task_id)
        compiler = ContextCompiler(db, settings.context_strategy)
        bundle = compiler.compile(
            p,
            task.contract["project"],
            task.contract["environment"],
            body.query if body.query is not None else task.contract["goal"],
            budget=body.budget,
            task_id=task_id,
        )
        compiler.validate(
            p, task.contract["project"], task.contract["environment"], bundle, task_id=task_id
        )
        return JSONResponse(bundle.as_dict(), headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/ops/summary")
    def operations_summary(p: Auth):
        from agent_py.operations import summary

        return JSONResponse(summary(service, p.tenant_id, p), headers={"Cache-Control": "no-store"})

    @app.get("/metrics")
    def metrics(authorization: Annotated[str | None, Header()] = None):
        from agent_py.operations import prometheus_snapshot

        secret = settings.metrics_secret.get_secret_value()
        if len(secret) < 32:
            raise DomainError("METRICS_DISABLED", "Configure a dedicated metrics credential", 503)
        if not authorization or not hmac.compare_digest(
            authorization.encode(), ("Bearer " + secret).encode()
        ):
            raise DomainError("FORBIDDEN", "Invalid metrics credential", 403)
        return Response(
            prometheus_snapshot(service),
            media_type="text/plain; version=0.0.4",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health/live")
    def live():
        return {
            "status": "ok",
            "release_id": service.release.id,
            "mode": settings.execution_mode,
            "repair_enabled": settings.allow_candidate_execution and settings.allow_model_api,
            "audit_signing_configured": settings.audit_signing_manifest is not None,
        }

    @app.get("/health/ready")
    def ready():
        try:
            service.release.check()
        except DomainError:
            return JSONResponse({"status": "release_mismatch"}, status_code=503)
        try:
            with db.engine.connect() as conn:
                conn.execute(text("SELECT 1 FROM tasks LIMIT 1"))
        except Exception:
            return JSONResponse({"status": "database_unavailable"}, status_code=503)
        if settings.auth_jwks_file is not None:
            from agent_py.auth_keys import load_keyset

            try:
                load_keyset(settings.auth_jwks_file)
            except DomainError:
                return JSONResponse({"status": "authentication_keys_unavailable"}, status_code=503)
            return {"status": "ready", "scope": "api-database-and-local-jwks"}
        return {"status": "ready", "scope": "api-database-only"}

    @app.post("/api/v1/tasks", status_code=202)
    def create(contract: TaskContract, p: Auth, idempotency_key: Annotated[str, Header()]):
        return task_json(service.create_task(p, contract, idempotency_key))

    @app.get("/api/v1/tasks")
    def tasks(p: Auth):
        with db.session(p.tenant_id) as s:
            candidates = s.scalars(
                select(Task)
                .where(Task.tenant_id == p.tenant_id)
                .order_by(Task.created_at.desc())
                .limit(100)
            ).all()
            allowed = []
            for t in candidates:
                try:
                    authorize(s, p, t)
                    allowed.append(task_json(t))
                except DomainError:
                    pass
            return {"items": allowed}

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str, p: Auth):
        t = service.get_task(p, task_id)
        with db.session(p.tenant_id) as s:
            ops = s.scalars(
                select(Operation).where(
                    Operation.tenant_id == p.tenant_id, Operation.task_id == task_id
                )
            ).all()
            artifacts = s.scalars(
                select(Artifact).where(
                    Artifact.tenant_id == p.tenant_id, Artifact.task_id == task_id
                )
            ).all()
            approvals = s.scalars(
                select(Approval)
                .join(Operation, Approval.operation_id == Operation.id)
                .where(Approval.tenant_id == p.tenant_id, Operation.task_id == task_id)
            ).all()
            return {
                **task_json(t),
                "operations": [operation_json(o) for o in ops],
                "artifacts": [{"id": a.id, "kind": a.kind, "digest": a.digest} for a in artifacts],
                "approvals": [
                    {
                        "id": a.id,
                        "operation_id": a.operation_id,
                        "status": a.status,
                        "payload_digest": a.payload_digest,
                        "expires_at": a.expires_at.isoformat(),
                    }
                    for a in approvals
                ],
            }

    @app.post("/api/v1/tasks/{task_id}/cancel")
    def cancel(task_id: str, p: Auth):
        service.stop(p, task_id)
        return task_json(service.get_task(p, task_id))

    @app.post("/api/v1/tasks/{task_id}/takeover")
    def takeover(task_id: str, p: Auth):
        service.stop(p, task_id, True)
        return task_json(service.get_task(p, task_id))

    @app.post("/api/v1/tasks/{task_id}/proposals", status_code=201)
    def propose(task_id: str, body: ProposalRequest, p: Auth):
        return operation_json(service.propose(p, task_id, body.step_key, body.action))

    @app.post("/api/v1/tasks/{task_id}/resume")
    def resume(task_id: str, body: ResumeRequest, p: Auth):
        return task_json(service.resume(p, task_id, body.expected_version))

    @app.post("/api/v1/approvals/{approval_id}/decisions")
    def decide(approval_id: str, body: ApprovalDecision, p: Auth):
        a = service.decide(p, approval_id, body.decision, body.expected_digest)
        return {"id": a.id, "status": a.status}

    @app.get("/api/v1/operations/{operation_id}")
    def get_operation(operation_id: str, p: Auth):
        with db.session(p.tenant_id) as s:
            op = tenant_get(s, Operation, operation_id, p.tenant_id)
            authorize(s, p, tenant_get(s, Task, op.task_id, p.tenant_id))
            return operation_json(op)

    @app.get("/api/v1/tasks/{task_id}/repair")
    def repair(task_id: str, p: Auth):
        from agent_py.repair import repair_details

        return {"repair": repair_details(service, p, task_id)}

    @app.get("/api/v1/tasks/{task_id}/recording")
    def recording(task_id: str, p: Auth, signed: bool = False):
        from agent_py.audit import export_audit

        data = export_audit(service, p, task_id)
        if signed:
            from agent_py.audit_signing import sign_recording

            if settings.audit_signing_manifest is None:
                raise DomainError(
                    "AUDIT_SIGNING_UNAVAILABLE", "Audit signing is not configured", 503
                )
            data = sign_recording(data, settings.audit_signing_manifest)
            service.get_task(p, task_id)
        return JSONResponse(
            data,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": 'attachment; filename="task-recording.json"',
            },
        )

    @app.post("/api/v1/tasks/{task_id}/repair/retry-verification", status_code=202)
    def retry_verification(task_id: str, body: VerificationRetryRequest, p: Auth):
        from agent_py.repair import RepairHarness

        return RepairHarness(service).retry_verification(p, task_id, body.attempt_id)

    @app.get("/api/v1/tasks/{task_id}/repair/attempts/{attempt_id}/patch")
    def repair_patch(task_id: str, attempt_id: str, p: Auth):
        from agent_py.db import RepairAttempt, RepairRun

        service.get_task(p, task_id)
        with db.session(p.tenant_id) as s:
            attempt = tenant_get(s, RepairAttempt, attempt_id, p.tenant_id)
            run = tenant_get(s, RepairRun, attempt.run_id, p.tenant_id)
            if run.task_id != task_id or not attempt.patch_artifact_id:
                raise DomainError("NOT_FOUND", "Patch not available for this task", 404)
        _, data = store.read(p, attempt.patch_artifact_id)
        return JSONResponse(json.loads(data), headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str, p: Auth):
        artifact, data = store.read(p, artifact_id)
        return Response(
            data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.id}.json"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/v1/tasks/{task_id}/events")
    async def events(
        task_id: str,
        p: Auth,
        request: Request,
        after: int = 0,
        last_event_id: Annotated[str | None, Header()] = None,
    ):
        try:
            cursor = int(last_event_id) if last_event_id is not None else after
            if cursor < 0:
                raise ValueError()
        except ValueError:
            raise DomainError("INVALID_CURSOR", "Event cursor must be a nonnegative integer", 422)

        from agent_py.event_feed import event_page

        token = request.headers["authorization"][7:]
        first = await asyncio.to_thread(event_page, service, token, task_id, cursor)

        async def stream():
            nonlocal cursor
            for iteration in range(120):
                if await request.is_disconnected():
                    break
                try:
                    rows, terminal = (
                        first
                        if iteration == 0
                        else await asyncio.to_thread(event_page, service, token, task_id, cursor)
                    )
                except DomainError as exc:
                    kind = (
                        "history_unavailable"
                        if exc.code in {"EVENT_GAP", "CURSOR_AHEAD"}
                        else "access_revoked"
                    )
                    yield f"event: {kind}\ndata: {{}}\n\n"
                    break
                for row in rows:
                    cursor = row["sequence"]
                    yield f"id: {cursor}\nevent: {row['type']}\ndata: {json.dumps(row['payload'])}\n\n"
                if terminal:
                    yield 'event: stream.closed\ndata: {"terminal":true}\n\n'
                    break
                yield ": heartbeat\n\n"
                if len(rows) < 200:
                    await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/webhooks/connector", status_code=202)
    async def webhook(request: Request):
        from agent_py.webhooks import receive

        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 1_000_000:
                raise DomainError("WEBHOOK_SIZE", "Webhook body exceeds limit", 413)
        event_id = receive(
            db,
            settings,
            bytes(data),
            request.headers.get("x-agent-timestamp", ""),
            request.headers.get("x-agent-signature", ""),
        )
        return {"event_id": event_id, "accepted": True}

    return app
