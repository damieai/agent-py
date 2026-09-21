"""Budget-partitioned, durable paired experiments over the existing investigation Harness."""

import json
import time
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, SecretStr, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from agent_py.adapters.simulation import DisabledLiveExecutor
from agent_py.artifacts import ArtifactStore
from agent_py.config import Settings
from agent_py.db import Database, Document, Grant, Operation, Reservation, Task
from agent_py.domain import (
    Contract,
    DomainError,
    InvestigationDecision,
    ModelDecision,
    Principal,
    TaskContract,
    digest,
)
from agent_py.experiment_datasets import Split, publish_json, read_snapshot
from agent_py.experiment_review import regular_file
from agent_py.investigation import InvestigationHarness
from agent_py.jsonio import load_json
from agent_py.model import AnthropicGateway
from agent_py.retrieval_experiments import exclusive_run, implementation_identity, percentile

WORKFLOWS = ("investigate", "investigation_loop")
TENANT = "investigation-experiment"
PRINCIPAL = Principal(
    tenant_id=TENANT,
    subject="reader",
    roles=["operator"],
    projects=["fixture"],
    environments=["lab"],
)
SHA = r"^[a-f0-9]{64}$"


class InvestigationExperimentConfig(Contract):
    format: Literal["agent-investigation-config/v1"] = "agent-investigation-config/v1"
    mode: Literal["synthetic", "live"] = "synthetic"
    model_id: str = Field(default="synthetic-investigation-v1", min_length=1, max_length=160)
    split: Split = "development"
    repeats: int = Field(default=1, ge=1, le=10)
    context_strategy: Literal["lexical", "bm25_rrf"] = "lexical"
    input_micro_per_token: int = Field(default=1, ge=1, le=100_000)
    output_micro_per_token: int = Field(default=2, ge=1, le=100_000)
    task_budget_micro_usd: int = Field(default=100_000, ge=1, le=100_000_000)
    total_budget_micro_usd: int = Field(default=400_000, ge=1, le=20_000_000_000)
    deadline_seconds: int = Field(default=1800, ge=30, le=86400)

    @model_validator(mode="after")
    def mode_model(self):
        if (self.mode == "synthetic") != (self.model_id == "synthetic-investigation-v1"):
            raise ValueError("Synthetic and live model identities must be distinct")
        return self


class ExperimentSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, **_):
        # No .env, host AGENT_*, file secrets or release/tool/platform credentials.
        return (init_settings,)


class LedgerCall(Contract):
    call_key: str = Field(min_length=1, max_length=160)
    maximum: int = Field(gt=0)
    actual: int | None = Field(default=None, ge=0)
    dispatched: bool
    request_digest: str | None = Field(default=None, pattern=SHA)
    decision_digest: str | None = Field(default=None, pattern=SHA)


class InvestigationRecord(Contract):
    format: Literal["agent-investigation-record/v1"] = "agent-investigation-record/v1"
    job_digest: str = Field(pattern=SHA)
    case_id: str
    workflow: Literal["investigate", "investigation_loop"]
    repeat: int = Field(ge=0, le=9)
    task_id: str
    status: Literal["ANALYSIS_READY", "BLOCKED", "FAILED", "UNKNOWN"]
    reason: str = Field(min_length=1, max_length=80)
    elapsed_ns: int = Field(ge=1)
    timing_scope: Literal["current_execution_segment"] = "current_execution_segment"
    recovered: bool
    spent_micro_usd: int = Field(ge=0)
    reserved_micro_usd: int = Field(ge=0)
    operations: int = Field(ge=0)
    calls: list[LedgerCall] = Field(max_length=3)
    analysis_digest: str | None = Field(default=None, pattern=SHA)
    cited_ids: list[str] = Field(default_factory=list, max_length=300)

    @model_validator(mode="after")
    def accounting(self):
        if len({c.call_key for c in self.calls}) != len(self.calls):
            raise ValueError("Duplicate ledger call")
        if self.spent_micro_usd != sum(c.actual or 0 for c in self.calls):
            raise ValueError("Spent does not match inference ledger")
        if self.reserved_micro_usd != sum(c.maximum for c in self.calls if c.actual is None):
            raise ValueError("Reserved does not match inference ledger")
        if self.analysis_digest is None and self.cited_ids:
            raise ValueError("Citations require an analysis artifact")
        if len(set(self.cited_ids)) != len(self.cited_ids):
            raise ValueError("Duplicate citation")
        if (self.status == "ANALYSIS_READY") != (self.analysis_digest is not None):
            raise ValueError("Analysis status requires its immutable artifact")
        if self.status == "ANALYSIS_READY" and (
            not self.calls
            or any(
                c.decision_digest is None
                or c.actual is None
                or not c.dispatched
                or c.request_digest is None
                for c in self.calls
            )
        ):
            raise ValueError("Analysis requires validated inference decisions")
        return self


def schedule(plan, cases):
    for case in cases:
        for repeat in range(plan["config"]["repeats"]):
            for workflow in WORKFLOWS[repeat % 2 :] + WORKFLOWS[: repeat % 2]:
                key = digest(
                    {"plan": digest(plan), "case": case.id, "repeat": repeat, "workflow": workflow}
                )
                yield case, workflow, repeat, key


def synthetic_response(request):
    """A labelled transport fixture, never a model-quality benchmark; sees no reference labels."""
    payload = json.loads(request.content)
    context = json.loads(payload["messages"][0]["content"])["context"]
    name = payload["tool_choice"]["name"]
    decision = {
        "summary": "Synthetic transport fixture; requires human review.",
        "hypotheses": ["Synthetic unverified hypothesis"],
        "evidence_ids": [d["id"] for d in context["documents"]][:1],
        "stop": True,
    }
    if name == "submit_investigation":
        more = context["round"] == 1
        decision.update(stop=not more, next_query="followup" if more else None)
    return httpx.Response(
        200,
        json={
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 30, "output_tokens": 10},
            "content": [{"type": "tool_use", "name": name, "input": decision}],
        },
    )


def build_service(directory, config, key):
    settings = ExperimentSettings(
        environment="test",
        execution_mode="live",
        database_url=f"sqlite:///{directory / 'state.db'}",
        artifact_root=directory / "artifacts",
        allow_model_api=True,
        model_id=config.model_id,
        model_api_key=SecretStr(key if config.mode == "live" else "synthetic-not-a-credential"),
        model_input_micro_per_token=config.input_micro_per_token,
        model_output_micro_per_token=config.output_micro_per_token,
        daily_budget_micro_usd=config.task_budget_micro_usd,
        context_strategy=config.context_strategy,
    )
    from agent_py.service import Service

    db = Database(settings.database_url)
    return Service(db, settings, DisabledLiveExecutor())


def contract_for(case, workflow, config):
    return TaskContract(
        kind="incident",
        workflow=workflow,
        goal=case.query,
        project="fixture",
        budget_micro_usd=config.task_budget_micro_usd,
        deadline_seconds=config.deadline_seconds,
    )


def validate_state(db, case, workflow, config, key):
    try:
        with db.session(TENANT) as s:
            tasks = s.scalars(select(Task)).all()
            body = contract_for(case, workflow, config).model_dump()
            if workflow == "investigate":
                body.pop("workflow")
            if (
                len(tasks) != 1
                or tasks[0].request_key != key
                or tasks[0].tenant_id != TENANT
                or tasks[0].principal != "reader"
                or tasks[0].contract != body
                or tasks[0].request_digest != digest(body)
            ):
                raise ValueError(
                    "Interrupted initialization or task drift requires operator recovery"
                )
            docs = s.scalars(select(Document)).all()
            actual = sorted(
                (
                    d.id,
                    d.tenant_id,
                    d.project,
                    d.source,
                    d.body,
                    d.version,
                    d.allowed_subjects,
                    d.revoked,
                    d.task_id,
                    d.valid_until,
                )
                for d in docs
            )
            expected = sorted(
                (
                    d.id,
                    TENANT,
                    "fixture",
                    d.source,
                    d.body,
                    digest(d.body),
                    ["reader"],
                    False,
                    None,
                    None,
                )
                for d in case.documents
            )
            if actual != expected:
                raise ValueError("Persisted input changed")
            grants = s.scalars(select(Grant)).all()
            if len(grants) != 1 or (
                grants[0].tenant_id,
                grants[0].subject,
                grants[0].project,
                grants[0].environment,
            ) != (TENANT, "reader", "fixture", "lab"):
                raise ValueError("Persisted grant changed")
    except SQLAlchemyError:
        raise ValueError("Unreadable recovery ledger; never recreate it") from None


def ensure_state(service, case, workflow, config, key, recover):
    if not recover:
        service.db.create_schema()
        (service.settings.artifact_root.parent / "state.db").chmod(0o600)
        with service.db.session(TENANT) as s:
            s.add(Grant(tenant_id=TENANT, subject="reader", project="fixture", environment="lab"))
            for doc in case.documents:
                s.add(
                    Document(
                        **doc.model_dump(),
                        tenant_id=TENANT,
                        project="fixture",
                        version=digest(doc.body),
                        allowed_subjects=["reader"],
                    )
                )
    else:
        validate_state(service.db, case, workflow, config, key)
    # Existing idempotent request binds the original contract and deadline on recovery.
    return service.create_task(PRINCIPAL, contract_for(case, workflow, config), key)


SAFE_REASONS = frozenset(
    {
        "HUMAN_REVIEW",
        "EVIDENCE_REQUIRED",
        "BUDGET_EXHAUSTED",
        "DAILY_BUDGET_EXHAUSTED",
        "DEADLINE",
        "INFERENCE_ALREADY_DISPATCHED",
        "MODEL_CALL_ALREADY_SETTLED",
        "MODEL_INCOMPLETE",
        "MODEL_SCHEMA",
        "UNSUPPORTED_CITATION",
        "MODEL_USAGE",
        "MODEL_OUTPUT_LIMIT",
        "INVESTIGATION_DRIFT",
        "INFERENCE_CONFLICT",
        "BUDGET_CONFLICT",
    }
)


def execute_job(directory, case, workflow, repeat, key, config, api_key, recover):
    started = time.perf_counter_ns()
    service = build_service(directory, config, api_key)
    try:
        task = ensure_state(service, case, workflow, config, key, recover)
        transport = (
            httpx.MockTransport(synthetic_response)
            if config.mode == "synthetic"
            else httpx.HTTPTransport(retries=0, trust_env=False)
        )
        gateway = AnthropicGateway(
            service.settings,
            service,
            config.input_micro_per_token,
            config.output_micro_per_token,
            transport,
        )
        harness = InvestigationHarness(service, gateway)
        status, reason, analysis = "BLOCKED", "TICK_LIMIT", None
        try:
            for _ in range(4):
                result = harness.tick(TENANT, task.id)
                wait = result.get("wait")
                if wait == "INVESTIGATION_CONTINUE":
                    continue
                reason = wait if wait in SAFE_REASONS else "TASK_STOPPED"
                if wait == "HUMAN_REVIEW":
                    _, raw = ArtifactStore(service.db, service.settings.artifact_root).read(
                        PRINCIPAL, result["artifact_id"]
                    )
                    analysis = json.loads(raw)
                    status = "ANALYSIS_READY"
                break
        except DomainError as exc:
            reason = exc.code if exc.code in SAFE_REASONS else "EXECUTION_FAILED"
            status = "BLOCKED"
        except Exception:
            reason, status = "EXECUTION_FAILED", "FAILED"
        with service.db.session(TENANT) as s:
            task = s.get(Task, task.id)
            reservations = s.scalars(
                select(Reservation)
                .where(Reservation.task_id == task.id)
                .order_by(Reservation.call_key)
            ).all()
            calls = [
                LedgerCall(
                    call_key=r.call_key,
                    maximum=r.maximum,
                    actual=r.actual,
                    dispatched=r.dispatched,
                    request_digest=r.request_digest,
                    decision_digest=digest(r.decision) if r.decision is not None else None,
                )
                for r in reservations
            ]
            if any(c.dispatched and c.decision_digest is None for c in calls):
                status, reason, analysis = "UNKNOWN", "NO_VALIDATED_INFERENCE_RESULT", None
            operations = s.scalar(select(func.count()).select_from(Operation))
            citations = []
            if analysis:
                rounds = analysis.get("rounds", [analysis])
                for row in rounds:
                    decision = row["decision"]
                    citations.extend(decision["evidence_ids"])
                    if decision.get("next_action"):
                        citations.extend(decision["next_action"]["evidence_ids"])
            record = InvestigationRecord(
                job_digest=key,
                case_id=case.id,
                workflow=workflow,
                repeat=repeat,
                task_id=task.id,
                status=status,
                reason=reason,
                elapsed_ns=max(1, time.perf_counter_ns() - started),
                recovered=recover,
                spent_micro_usd=task.spent,
                reserved_micro_usd=task.reserved,
                operations=operations,
                calls=calls,
                analysis_digest=digest(analysis) if analysis else None,
                cited_ids=sorted(set(citations)),
            )
        if analysis:
            path = directory / "analysis.json"
            if path.exists():
                if digest(load_json(regular_file(path), 10_000_000)) != record.analysis_digest:
                    raise ValueError("Previously published analysis changed")
            else:
                publish_json(path, analysis, max_bytes=10_000_000)
        return record
    finally:
        service.telemetry.close()
        service.db.engine.dispose()


def validate_record(record, directory, case, workflow, repeat, key):
    if (record.job_digest, record.case_id, record.workflow, record.repeat) != (
        key,
        case.id,
        workflow,
        repeat,
    ):
        raise ValueError("Experiment record identity mismatch")
    if not set(record.cited_ids) <= {d.id for d in case.documents}:
        raise ValueError("Citation outside frozen dataset")
    allowed = (
        {"investigation:v1"}
        if workflow == "investigate"
        else {f"investigation-loop:v1:{n}" for n in (1, 2, 3)}
    )
    if not {c.call_key for c in record.calls} <= allowed:
        raise ValueError("Unexpected model call")
    if record.analysis_digest:
        analysis = load_json(regular_file(directory / "analysis.json"), 10_000_000)
        if not isinstance(analysis, dict) or digest(analysis) != record.analysis_digest:
            raise ValueError("Analysis shape or digest mismatch")
        if (
            analysis.get("executed") is not False
            or analysis.get("requires_human_review") is not True
        ):
            raise ValueError("Analysis must remain a proposal for review")
        rows = analysis.get("rounds") if workflow == "investigation_loop" else [analysis]
        if not isinstance(rows, list) or len(rows) != len(record.calls):
            raise ValueError("Analysis does not cover validated ledger calls")
        citations = set()
        by_key = {c.call_key: c for c in record.calls}
        for ordinal, row in enumerate(rows, 1):
            if not isinstance(row, dict) or "decision" not in row or "context" not in row:
                raise ValueError("Invalid analysis round")
            schema = InvestigationDecision if workflow == "investigation_loop" else ModelDecision
            decision = schema.model_validate(row["decision"])
            call_key = (
                f"investigation-loop:v1:{ordinal}"
                if workflow == "investigation_loop"
                else "investigation:v1"
            )
            if (
                call_key not in by_key
                or digest(decision.model_dump()) != by_key[call_key].decision_digest
            ):
                raise ValueError("Analysis decision differs from inference ledger")
            cited = set(decision.evidence_ids)
            if isinstance(decision, ModelDecision) and decision.next_action:
                cited.update(decision.next_action.evidence_ids)
            context = row["context"]
            if not isinstance(context, dict) or not isinstance(context.get("documents"), list):
                raise ValueError("Invalid analysis context")
            docs = context["documents"]
            if any(not isinstance(d, dict) or not isinstance(d.get("id"), str) for d in docs):
                raise ValueError("Invalid context document")
            if not cited <= {d["id"] for d in docs} <= {d.id for d in case.documents}:
                raise ValueError("Citation not supplied to this model round")
            citations.update(cited)
        if citations != set(record.cited_ids):
            raise ValueError("Recorded citations differ from raw analysis")


def read_result(directory, case, workflow, repeat, key):
    envelope = load_json(regular_file(directory / "result.json"), 200_000)
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"record", "content_digest"}
        or digest(envelope["record"]) != envelope["content_digest"]
    ):
        raise ValueError("Investigation result digest mismatch")
    record = InvestigationRecord.model_validate(envelope["record"])
    validate_record(record, directory, case, workflow, repeat, key)
    return record


def summarize(config, plan, cases, records):
    expected = len(cases) * config.repeats * 2
    violations = [
        r.job_digest
        for r in records
        if r.operations or r.spent_micro_usd + r.reserved_micro_usd > config.task_budget_micro_usd
    ]
    results = []
    for workflow in WORKFLOWS:
        rows = [r for r in records if r.workflow == workflow]
        times = [r.elapsed_ns / 1_000_000 for r in rows if not r.recovered]
        results.append(
            {
                "workflow": workflow,
                "recorded": len(rows),
                "reasons": {
                    reason: sum(r.reason == reason for r in rows)
                    for reason in sorted({r.reason for r in rows})
                },
                "expected": expected // 2,
                "analysis_ready": sum(r.status == "ANALYSIS_READY" for r in rows),
                "unknown": sum(r.status == "UNKNOWN" for r in rows),
                "blocked_or_failed": sum(r.status in {"BLOCKED", "FAILED"} for r in rows),
                "ledger_spent_micro_usd": sum(r.spent_micro_usd for r in rows),
                "unsettled_reserved_micro_usd": sum(r.reserved_micro_usd for r in rows),
                "dispatched_calls": sum(c.dispatched for r in rows for c in r.calls),
                "job_p50_ms": percentile(times, 0.5),
                "job_p95_ms": percentile(times, 0.95),
                "latency_samples": len(times),
                "recovered_latency_excluded": sum(r.recovered for r in rows),
            }
        )
    paired = []
    indexed = {(r.case_id, r.workflow, r.repeat): r for r in records}
    for case in cases:
        pairs = [
            (
                indexed.get((case.id, "investigate", n)),
                indexed.get((case.id, "investigation_loop", n)),
            )
            for n in range(config.repeats)
        ]
        ready = all(a and b and a.status == b.status == "ANALYSIS_READY" for a, b in pairs)
        paired.append(
            {
                "case_id": case.id,
                "entity_group": case.entity_group,
                "template_family": case.template_family,
                "complete_pair": ready,
                "mean_ledger_cost_delta_micro_usd": sum(
                    b.spent_micro_usd - a.spent_micro_usd for a, b in pairs
                )
                / len(pairs)
                if ready
                else None,
            }
        )
    return {
        "format": "agent-investigation-experiment-report/v1",
        "plan_digest": digest(plan),
        "mode": config.mode,
        "execution_status": "INVALID"
        if violations
        else "COMPLETE"
        if len(records) == expected and all(r.status == "ANALYSIS_READY" for r in records)
        else "INCOMPLETE",
        "expected_jobs": expected,
        "recorded_jobs": len(records),
        "pending_jobs": expected - len(records),
        "preallocated_budget_micro_usd": expected * config.task_budget_micro_usd,
        "ledger_exposure_micro_usd": sum(r.spent_micro_usd + r.reserved_micro_usd for r in records),
        "ledger_exposure_scope": "published_job_records_only",
        "cost_evidence_complete": len(records) == expected,
        "hard_gate_violations": violations,
        "results": results,
        "paired": paired,
        "answer_quality": "NOT_ASSESSED",
        "citation_correctness": "NOT_ASSESSED",
        "task_success": "NOT_ASSESSED",
        "provider_billing": "NOT_INCURRED" if config.mode == "synthetic" else "NOT_RECONCILED",
        "release_decision": "NOT_ASSESSED",
        "langfuse": "NOT_UPLOADED",
        "limitations": [
            "Analysis ready means a schema-validated report for human review, not task success",
            "Citation membership is checked; semantic support and answer quality are not scored",
            "Ledger charges may include conservative maximums for unvalidated responses",
            "Static equal per-task budget ceilings do not imply equal realized compute",
            "Synthetic provider replies and usage cannot establish model quality or real costs",
            "Recovered segments are excluded from latency percentiles; no end-to-end recovery latency claim",
        ],
    }


def run_investigation_experiment(
    snapshot_path: Path,
    config_path: Path,
    output: Path,
    *,
    resume=False,
    allow_model_api=False,
    api_key="",
):
    snapshot = read_snapshot(snapshot_path)
    config = InvestigationExperimentConfig.model_validate(load_json(config_path))
    if config.mode == "live" and (not allow_model_api or not api_key):
        raise ValueError(
            "Live inference requires explicit export/paid-call opt-in and API credentials"
        )
    cases = [c for c in snapshot.dataset.cases if c.split == config.split]
    if not cases or len(cases) * config.repeats * 2 > 200:
        raise ValueError("Investigation experiment requires 1..200 jobs")
    if any(c.context_budget != 6000 or len(c.query) < 5 for c in cases):
        raise ValueError(
            "Investigation Harness v1 requires context budget 6000 and goals of at least 5 characters"
        )
    if (
        len(cases) * config.repeats * 2 * config.task_budget_micro_usd
        > config.total_budget_micro_usd
    ):
        raise ValueError("Total budget must cover both arms and every repeat before execution")
    identity = implementation_identity()
    plan = {
        "format": "agent-investigation-experiment/v1",
        "dataset_digest": snapshot.content_digest,
        "implementation": identity,
        "config": config.model_dump(),
        "concurrency": 1,
        "workflows": list(WORKFLOWS),
    }
    if resume:
        if output.is_symlink() or not output.is_dir():
            raise ValueError("Resume requires an existing real directory")
    else:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with exclusive_run(output):
        if resume:
            if (
                digest(load_json(regular_file(output / "plan.json"), 2_000_000)) != digest(plan)
                or read_snapshot(regular_file(output / "dataset.json")) != snapshot
            ):
                raise ValueError("Frozen investigation plan, dataset or implementation changed")
        else:
            publish_json(output / "dataset.json", snapshot.model_dump(), max_bytes=4_000_000)
            publish_json(output / "plan.json", plan)
            (output / "jobs").mkdir(mode=0o700)
        root = output / "jobs"
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Invalid job directory")
        planned = list(schedule(plan, cases))
        expected = {key for _, _, _, key in planned}
        if any(p.name not in expected for p in root.iterdir()):
            raise ValueError("Unexpected experiment job")
        records, pending = [], []
        # Verify all old results and recovery prerequisites before any new model call.
        for case, workflow, repeat, key in planned:
            directory = root / key
            if directory.is_symlink():
                raise ValueError("Job directory cannot be a link")
            started = directory.exists()
            if started:
                if load_json(regular_file(directory / "started.json")) != {"job_digest": key}:
                    raise ValueError("Job start identity changed")
                if (directory / "result.json").exists() or (directory / "result.json").is_symlink():
                    records.append(read_result(directory, case, workflow, repeat, key))
                    continue
                regular_file(directory / "state.db")  # Never recreate a lost paid-call ledger.
                db = Database(f"sqlite:///{directory / 'state.db'}")
                try:
                    validate_state(db, case, workflow, config, key)
                finally:
                    db.engine.dispose()
            pending.append((case, workflow, repeat, key, started))
        report = summarize(config, plan, cases, records)
        publish_json(output / "report.json", report, replace=True)
        for case, workflow, repeat, key, recover in pending:
            if report["hard_gate_violations"]:
                break
            directory = root / key
            if not recover:
                directory.mkdir(mode=0o700)
                publish_json(directory / "started.json", {"job_digest": key})
            record = execute_job(directory, case, workflow, repeat, key, config, api_key, recover)
            validate_record(record, directory, case, workflow, repeat, key)
            publish_json(
                directory / "result.json",
                {"record": record.model_dump(), "content_digest": digest(record.model_dump())},
            )
            records.append(record)
            report = summarize(config, plan, cases, records)
            publish_json(output / "report.json", report, replace=True)
        if implementation_identity() != identity:
            report["execution_status"], report["error"] = "INVALID", "IMPLEMENTATION_CHANGED"
            publish_json(output / "report.json", report, replace=True)
        return report
