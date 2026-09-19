import hashlib
import json
import os
import subprocess
import sys

import pytest

from agent_py.artifacts import ArtifactStore
from agent_py.domain import DomainError
from agent_py.patches import FileEdit
from agent_py.sandbox import DockerSandbox, SandboxResult
from agent_py.verification import VerificationRunner, snapshot_python

IMAGE = "python@sha256:" + "a" * 64


def prepared(env, tmp_path):
    service = env[0]
    source = tmp_path / "repo"
    (source / "src").mkdir(parents=True)
    original = "def value():\n    return 1\n"
    (source / "src/app.py").write_text(original)
    oracle = tmp_path / "trusted"
    oracle.mkdir()
    (oracle / "test_app.py").write_text(
        "from app import value\ndef test_value(): assert value() == 2\n"
    )
    service.settings.sandbox_root = tmp_path / "sandboxes"
    service.settings.sandbox_oracle = oracle
    service.settings.sandbox_image = IMAGE
    edit = FileEdit(
        path="src/app.py",
        original_sha256=hashlib.sha256(original.encode()).hexdigest(),
        content=original.replace("return 1", "return 2"),
    )
    return source, edit


@pytest.mark.parametrize(
    "before,after,outcome",
    [
        (SandboxResult(1, "failed"), SandboxResult(0, "passed"), "REGRESSION_FIXED"),
        (SandboxResult(0, "passed"), SandboxResult(0, "passed"), "BASELINE_NOT_REPRODUCED"),
        (SandboxResult(1, "failed"), SandboxResult(1, "failed"), "CANDIDATE_FAILED"),
        (SandboxResult(127, "no docker"), SandboxResult(0, "passed"), "INCONCLUSIVE"),
        (SandboxResult(1, "failed"), SandboxResult(0, "partial", "OUTPUT_LIMIT"), "INCONCLUSIVE"),
    ],
)
def test_verification_snapshots_and_outcome_gates(env, task, tmp_path, before, after, outcome):
    source, edit = prepared(env, tmp_path)
    calls = []

    class Sandbox:
        def __init__(self, image, root):
            assert image == IMAGE

        def verify(self, workspace, *, oracle, timeout_seconds):
            assert oracle not in workspace.parents and workspace not in oracle.parents
            assert (oracle / "test_app.py").read_text().endswith("== 2\n")
            calls.append((workspace / "src/app.py").read_text())
            return before if len(calls) == 1 else after

    result = VerificationRunner(env[0], Sandbox).run(env[1], task.id, source, [edit])
    assert result["outcome"] == outcome
    assert "return 1" in calls[0] and "return 2" in calls[1]
    assert "return 1" in (source / "src/app.py").read_text()
    assert list(env[0].settings.sandbox_root.iterdir()) == []
    artifact, content = ArtifactStore(env[0].db, env[0].settings.artifact_root).read(
        env[1], result["artifact_id"]
    )
    assert artifact.kind == "candidate-verification"
    assert json.loads(content)["business_verified"] is False
    with pytest.raises(DomainError, match="verification"):
        env[0].finish("t1", task.id)


def test_snapshot_rejects_symlinks_and_oversized_source(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "app.py").symlink_to(tmp_path / "outside")
    with pytest.raises(DomainError, match="symlinks"):
        snapshot_python(source, tmp_path / "copy1")
    (source / "app.py").unlink()
    (source / "app.py").write_bytes(b"x" * 2_000_001)
    with pytest.raises(DomainError, match="2 MB"):
        snapshot_python(source, tmp_path / "copy2")


def test_oracle_and_container_command_are_independent(tmp_path):
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    oracle = tmp_path / "oracle"
    oracle.mkdir()
    sandbox = DockerSandbox(IMAGE, tmp_path)
    args = sandbox.command(workspace, oracle, "test", "python")
    assert "--pull=never" in args and "--log-driver=none" in args
    assert "--network=none" in args and "--read-only" in args
    assert f"type=bind,src={oracle},dst=/oracle,readonly" in args
    assert "--noconftest" in args[-1] and "'/dev/null'" in args[-1]
    with pytest.raises(ValueError, match="independent"):
        sandbox.command(workspace, workspace, "test", "python")
    with pytest.raises(ValueError, match="oracle"):
        sandbox.verify(workspace)


@pytest.mark.parametrize(
    "script,timeout,limit",
    [
        ("import os; os.write(1, b'x' * 10000)", 5, "OUTPUT_LIMIT"),
        ("import time; time.sleep(30)", 1, "TIMEOUT"),
    ],
)
def test_output_and_time_limits_kill_process(tmp_path, monkeypatch, script, timeout, limit):
    # Only these fixed trusted programs run on the host; no candidate code is executed.
    workspace, oracle = tmp_path / "candidate", tmp_path / "oracle"
    workspace.mkdir()
    oracle.mkdir()
    original = subprocess.Popen
    killed = []

    def launch(args, **kwargs):
        assert args[:2] == ["docker", "run"]
        return original([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr("agent_py.sandbox.subprocess.Popen", launch)
    monkeypatch.setattr("agent_py.sandbox.subprocess.run", lambda args, **kw: killed.append(args))
    result = DockerSandbox(IMAGE, tmp_path).verify(
        workspace, oracle=oracle, timeout_seconds=timeout, output_limit=100
    )
    assert result.limit == limit and len(result.output) <= 100
    assert killed[0][:2] == ["docker", "kill"]


def test_missing_docker_has_no_host_fallback(tmp_path, monkeypatch):
    workspace, oracle = tmp_path / "candidate", tmp_path / "oracle"
    workspace.mkdir()
    oracle.mkdir()

    def absent(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr("agent_py.sandbox.subprocess.Popen", absent)
    with pytest.raises(FileNotFoundError):
        DockerSandbox(IMAGE, tmp_path).verify(workspace, oracle=oracle)


@pytest.mark.sandbox
@pytest.mark.skipif(
    not os.getenv("AGENT_TEST_SANDBOX_IMAGE"), reason="Pinned Docker test image opt-in"
)
def test_real_container_reproduces_and_verifies_patch(env, task, tmp_path):
    source, edit = prepared(env, tmp_path)
    env[0].settings.sandbox_image = os.environ["AGENT_TEST_SANDBOX_IMAGE"]
    result = VerificationRunner(env[0]).run(env[1], task.id, source, [edit])
    assert result["outcome"] == "REGRESSION_FIXED"


def test_patch_aliases_cannot_edit_same_file_twice(env, tmp_path):
    from agent_py.patches import apply_patch

    source, edit = prepared(env, tmp_path)
    alias = edit.model_copy(update={"path": "src/./app.py"})
    with pytest.raises(DomainError, match="unique"):
        apply_patch(source, [edit, alias])
    assert "return 1" in (source / "src/app.py").read_text()
