"""Materialize bounded, version-checked patches. Never execute candidate code here."""

import hashlib
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from agent_py.domain import DomainError


class FileEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    original_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: str = Field(max_length=200_000)


def validate_patch(workspace: Path, edits: list[FileEdit]) -> list[tuple[Path, bytes]]:
    root = workspace.resolve()
    if not 1 <= len(edits) <= 20 or sum(len(e.content.encode()) for e in edits) > 1_000_000:
        raise DomainError("PATCH_LIMIT", "Patch exceeds file or byte limit", 422)
    result, seen = [], set()
    for edit in edits:
        relative = PurePosixPath(edit.path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in edit.path
            or not relative.parts
            or relative.parts[0] != "src"
            or relative.suffix != ".py"
            or edit.path in seen
        ):
            raise DomainError("PATCH_SCOPE", "Only unique source Python files may be edited", 403)
        seen.add(edit.path)
        path = root / edit.path
        if root not in path.resolve().parents or any(
            p.is_symlink() for p in [path, *path.parents] if p != root
        ):
            raise DomainError("PATCH_SYMLINK", "Symlinks are not editable", 403)
        try:
            previous = path.read_bytes()
        except OSError:
            raise DomainError("PATCH_SOURCE", "Source file is unavailable", 409)
        if hashlib.sha256(previous).hexdigest() != edit.original_sha256:
            raise DomainError("PATCH_STALE", "Source changed since patch was proposed", 409)
        result.append((path, edit.content.encode()))
    return result


def apply_patch(workspace: Path, edits: list[FileEdit]):
    """Caller owns an exclusive disposable workspace; discard it if interrupted."""
    changes = validate_patch(workspace, edits)
    for path, data in changes:
        path.write_bytes(data)
    return {"files": [edit.path for edit in edits], "verification": "REQUIRED"}
