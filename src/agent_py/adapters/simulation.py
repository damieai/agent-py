"""An independent durable authority, not a mock of the local action ledger."""

import json
import sqlite3
from pathlib import Path

from agent_py.domain import DomainError, digest


class UnknownOutcome(Exception):
    pass


class ConfirmedFailure(Exception):
    pass


class SimulatedSystem:
    supported_tools = frozenset(
        {"create_pr", "trigger_ci", "merge_pr", "deploy", "rollback", "runbook"}
    )

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS receipts (
                    tenant TEXT, operation TEXT, digest TEXT, result TEXT,
                    PRIMARY KEY (tenant, operation));
                CREATE TABLE IF NOT EXISTS resources (
                    tenant TEXT, resource TEXT, body TEXT, PRIMARY KEY (tenant, resource));
                CREATE TABLE IF NOT EXISTS faults (
                    tenant TEXT, operation TEXT, mode TEXT, remaining INTEGER,
                    PRIMARY KEY (tenant, operation));
            """)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def inject(self, tenant: str, operation: str, mode: str, remaining: int = 1):
        if mode not in {"response_lost", "reject", "query_hidden"}:
            raise ValueError("Unsupported fault")
        with self.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO faults VALUES (?, ?, ?, ?)",
                (tenant, operation, mode, remaining),
            )

    def snapshot(self, tenant: str, resource: str) -> dict:
        with self.connect() as c:
            row = c.execute(
                "SELECT body FROM resources WHERE tenant=? AND resource=?", (tenant, resource)
            ).fetchone()
            return (
                json.loads(row[0])
                if row
                else {
                    "revision": 1,
                    "candidate_sha": digest("fixed-candidate"),
                    "base_sha": digest("baseline"),
                    "image_digest": "sha256:" + digest("healthy"),
                    "replicas": 2,
                    "healthy": False,
                    "effect_count": 0,
                }
            )

    def query(self, tenant: str, operation: str) -> dict | None:
        with self.connect() as c:
            fault = c.execute(
                "SELECT * FROM faults WHERE tenant=? AND operation=?", (tenant, operation)
            ).fetchone()
            if fault and fault["mode"] == "query_hidden" and fault["remaining"] > 0:
                c.execute(
                    "UPDATE faults SET remaining=remaining-1 WHERE tenant=? AND operation=?",
                    (tenant, operation),
                )
                return None
            row = c.execute(
                "SELECT result FROM receipts WHERE tenant=? AND operation=?", (tenant, operation)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def execute(self, tenant: str, operation: str, tool: str, resource: str, params: dict) -> dict:
        payload_hash = digest({"tool": tool, "resource": resource, "params": params})
        # Resource and receipt commit atomically at the simulated remote authority.
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute(
                "SELECT * FROM receipts WHERE tenant=? AND operation=?", (tenant, operation)
            ).fetchone()
            if existing:
                if existing["digest"] != payload_hash:
                    raise ConfirmedFailure("IDEMPOTENCY_CONFLICT")
                return json.loads(existing["result"])
            fault = c.execute(
                "SELECT * FROM faults WHERE tenant=? AND operation=?", (tenant, operation)
            ).fetchone()
            if fault and fault["mode"] == "reject":
                raise ConfirmedFailure("REMOTE_REJECTED")
            row = c.execute(
                "SELECT body FROM resources WHERE tenant=? AND resource=?", (tenant, resource)
            ).fetchone()
            state = json.loads(row[0]) if row else self.snapshot(tenant, resource)
            if "expected_revision" in params and params["expected_revision"] != state["revision"]:
                raise ConfirmedFailure("RESOURCE_CONFLICT")
            if tool == "merge_pr" and (
                params["source_sha"] != state["candidate_sha"]
                or params["target_sha"] != state["base_sha"]
            ):
                raise ConfirmedFailure("COMMIT_CHANGED")
            if tool == "create_pr":
                state["candidate_sha"] = params["candidate_sha"]
            elif tool in {"deploy", "rollback"}:
                state["image_digest"] = params["image_digest"]
                state["healthy"] = True
            elif tool == "runbook":
                state["replicas"] = params["replicas"]
                state["healthy"] = True
            state["revision"] += 1
            state["effect_count"] += 1
            result = {
                "external_id": "sim-" + operation,
                "confirmed": True,
                "resource": resource,
                "state": state,
                "simulation": True,
            }
            c.execute(
                "INSERT OR REPLACE INTO resources VALUES (?, ?, ?)",
                (tenant, resource, json.dumps(state)),
            )
            c.execute(
                "INSERT INTO receipts VALUES (?, ?, ?, ?)",
                (tenant, operation, payload_hash, json.dumps(result)),
            )
        if fault and fault["mode"] == "response_lost":
            raise UnknownOutcome("REMOTE_RESPONSE_LOST")
        return result


class DisabledLiveExecutor:
    """Fail closed until provider-specific write guarantees have been certified."""

    supported_tools = frozenset()

    def execute(self, *args, **kwargs):
        raise DomainError(
            "LIVE_WRITES_DISABLED", "Live write capability has not been certified", 503
        )

    def query(self, *args, **kwargs):
        raise DomainError(
            "LIVE_RECONCILIATION_UNCONFIGURED", "Configure the authoritative query adapter", 503
        )
