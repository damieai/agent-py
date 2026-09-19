"""Offline retrieval ablation over an explicit, operator-owned relevance fixture."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_py.context import ContextCompiler
from agent_py.db import Database, Document, Grant
from agent_py.domain import Principal, digest


class EvidenceFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=36)
    source: str = Field(min_length=1, max_length=300)
    body: str = Field(max_length=200_000)


class QueryFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=8000)
    relevant: list[str] = Field(min_length=1, max_length=100)
    budget: int = Field(default=6000, ge=1, le=100_000)


class RetrievalFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=80)
    documents: list[EvidenceFixture] = Field(min_length=1, max_length=1000)
    queries: list[QueryFixture] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def references(self):
        ids = {d.id for d in self.documents}
        if len(ids) != len(self.documents) or len({q.id for q in self.queries}) != len(
            self.queries
        ):
            raise ValueError("Fixture IDs must be unique")
        if any(not set(q.relevant) <= ids for q in self.queries):
            raise ValueError("Relevance labels must reference fixture documents")
        return self


def evaluate_retrieval(dataset: Path, output: Path):
    with dataset.open("rb") as stream:
        raw = stream.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("Retrieval fixture exceeds 2 MB")
    fixture = RetrievalFixture.model_validate_json(raw)
    principal = Principal(
        tenant_id="retrieval-eval",
        subject="evaluator",
        roles=[],
        projects=["fixture"],
        environments=["lab"],
    )
    results = []
    with TemporaryDirectory(prefix="agent-retrieval-") as directory:
        db = Database(f"sqlite:///{directory}/fixture.db")
        try:
            db.create_schema()
            with db.session(principal.tenant_id) as s:
                s.add(
                    Grant(
                        tenant_id=principal.tenant_id,
                        subject=principal.subject,
                        project="fixture",
                        environment="lab",
                    )
                )
                for doc in fixture.documents:
                    s.add(
                        Document(
                            **doc.model_dump(),
                            tenant_id=principal.tenant_id,
                            project="fixture",
                            version=digest(doc.body),
                            allowed_subjects=[principal.subject],
                        )
                    )
            for strategy in ("none", "lexical", "bm25_rrf"):
                rows = []
                for query in fixture.queries:
                    documents, byte_count = [], 0
                    if strategy != "none":
                        compiler = ContextCompiler(db, strategy)
                        bundle = compiler.compile(
                            principal, "fixture", "lab", query.query, budget=query.budget
                        )
                        compiler.validate(principal, "fixture", "lab", bundle)
                        documents, byte_count = bundle.documents, bundle.estimated_tokens
                    ids = list(dict.fromkeys(d["id"] for d in documents))
                    relevant = set(query.relevant)
                    hits = relevant & set(ids)
                    reciprocal = next(
                        (1 / rank for rank, id in enumerate(ids, 1) if id in relevant), 0
                    )
                    rows.append(
                        {
                            "id": query.id,
                            "retrieved": ids,
                            "recall": len(hits) / len(relevant),
                            "precision": len(hits) / len(ids) if ids else 0,
                            "reciprocal_rank": reciprocal,
                            "context_bytes": byte_count,
                            "budget": query.budget,
                            "chunks": len(documents),
                        }
                    )
                results.append(
                    {
                        "strategy": strategy,
                        "queries": rows,
                        "mean_recall": sum(r["recall"] for r in rows) / len(rows),
                        "mean_precision": sum(r["precision"] for r in rows) / len(rows),
                        "mrr": sum(r["reciprocal_rank"] for r in rows) / len(rows),
                    }
                )
        finally:
            db.engine.dispose()
    report = {
        "suite": "retrieval-ablation-v1",
        "dataset": fixture.name,
        "dataset_digest": digest(fixture.model_dump()),
        "not_a_model_benchmark": True,
        "limitation": "Authored development fixtures, not held-out production relevance. "
        "Document-level labels do not prove passage sufficiency or answer quality.",
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report
