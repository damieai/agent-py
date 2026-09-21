"""Content-addressed local experiment inputs; labels never become retrieval context."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from agent_py.domain import Contract, digest
from agent_py.jsonio import load_json
from agent_py.retrieval_evaluation import EvidenceFixture

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")]
Split = Literal["development", "calibration", "holdout"]
Strategy = Literal["none", "lexical", "bm25_rrf"]


class ExperimentCase(Contract):
    id: Identifier
    split: Split
    entity_group: Identifier
    template_family: Identifier
    query: str = Field(min_length=1, max_length=8000)
    documents: list[EvidenceFixture] = Field(min_length=1, max_length=100)
    relevant: list[str] = Field(min_length=1, max_length=100)
    context_budget: int = Field(default=6000, ge=1, le=100_000)

    @model_validator(mode="after")
    def references(self):
        ids = {d.id for d in self.documents}
        if len(ids) != len(self.documents) or len(set(self.relevant)) != len(self.relevant):
            raise ValueError("Document and reference IDs must be unique")
        if not set(self.relevant) <= ids or not self.query.strip():
            raise ValueError("References must exist and query must not be blank")
        return self

    def input_identity(self):
        def normalize(value):
            return " ".join(value.casefold().split())

        return digest(
            {
                "query": normalize(self.query),
                "bodies": sorted(normalize(d.body) for d in self.documents),
            }
        )


class ExperimentDataset(Contract):
    schema_version: Literal[1] = 1
    name: Identifier
    provenance: Literal["synthetic", "authorized_redacted"]
    cases: list[ExperimentCase] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def separation(self):
        ids, inputs, entities, families = set(), set(), {}, {}
        for case in self.cases:
            identity = case.input_identity()
            if case.id in ids or identity in inputs:
                raise ValueError("Duplicate case ID or normalized input")
            ids.add(case.id)
            inputs.add(identity)
            for groups, key in ((entities, case.entity_group), (families, case.template_family)):
                if key in groups and groups[key] != case.split:
                    raise ValueError("Entity or template family crosses dataset splits")
                groups[key] = case.split
        return self


class DatasetSnapshot(Contract):
    format: Literal["agent-experiment-dataset/v1"] = "agent-experiment-dataset/v1"
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset: ExperimentDataset

    @model_validator(mode="after")
    def integrity(self):
        if self.content_digest != digest(self.dataset.model_dump()):
            raise ValueError("Dataset digest mismatch")
        return self


def publish_json(path: Path, value, *, replace=False, max_bytes=None):
    """Private, atomic publication. Immutable artifacts use link/no-clobber semantics."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()
    if max_bytes is not None and len(raw) > max_bytes:
        raise ValueError("Published JSON exceeds limit")
    descriptor, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def freeze_dataset(source: Path, output: Path):
    dataset = ExperimentDataset.model_validate(load_json(source, 2_000_000))
    snapshot = DatasetSnapshot(content_digest=digest(dataset.model_dump()), dataset=dataset)
    publish_json(output, snapshot.model_dump(), max_bytes=4_000_000)
    return snapshot


def read_snapshot(path: Path):
    return DatasetSnapshot.model_validate(load_json(path, 4_000_000))


def file_digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
