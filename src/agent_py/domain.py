import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TaskKind(StrEnum):
    REPAIR = "repair"
    INCIDENT = "incident"


class Principal(Contract):
    tenant_id: str
    subject: str
    roles: list[str]
    projects: list[str]
    environments: list[str]


class TaskContract(Contract):
    kind: Literal["repair", "incident"]
    workflow: Literal["investigate", "repair_candidate"] = "investigate"
    goal: str = Field(min_length=5, max_length=8000)
    project: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    environment: Literal["lab", "staging", "production"] = "lab"
    resource: str = Field(default="demo-service", pattern=r"^[a-zA-Z0-9_.:/-]{1,160}$")
    budget_micro_usd: int = Field(default=1_000_000, gt=0, le=100_000_000)
    deadline_seconds: int = Field(default=1800, ge=30, le=86400)
    release_id: Literal["agent-v1"] = "agent-v1"

    @model_validator(mode="after")
    def repair_workflow(self):
        if self.workflow == "repair_candidate" and self.kind != "repair":
            raise ValueError("Candidate repair workflow requires repair task kind")
        return self


class ActionProposal(Contract):
    tool: Literal["create_pr", "trigger_ci", "merge_pr", "deploy", "rollback", "runbook"]
    resource: str = Field(min_length=1, max_length=160)
    parameters: dict[str, Any]
    evidence_ids: list[str] = Field(default_factory=list)


class ApprovalDecision(Contract):
    decision: Literal["approve", "reject"]
    expected_digest: str


class EvidenceRef(Contract):
    source: str
    source_version: str
    location: str
    text: str
    observed_at: str
    sensitivity: Literal["public", "internal", "restricted"] = "internal"


class ModelDecision(Contract):
    summary: str
    hypotheses: list[str]
    evidence_ids: list[str]
    next_action: ActionProposal | None = None
    stop: bool = False

    @model_validator(mode="after")
    def check_stop(self):
        if self.stop and self.next_action:
            raise ValueError("A stopping decision cannot request an action")
        return self


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


APPROVAL_TOOLS = frozenset({"merge_pr", "deploy", "rollback", "runbook"})
ACTION_STATES = frozenset({"NOT_SUBMITTED", "PENDING", "SUCCEEDED", "FAILED", "UNKNOWN"})


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)
