import os
import re
import selectors
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    output: str
    limit: str | None = None


class DockerSandbox:
    """Fail closed without Docker; use an operator-owned oracle outside candidate source."""

    def __init__(self, image: str, root: Path):
        if not re.fullmatch(r"[a-zA-Z0-9./_:-]+@sha256:[a-f0-9]{64}", image):
            raise ValueError("Sandbox image must be pinned by digest")
        self.image, self.root = image, root.resolve()

    def command(self, workspace: Path, oracle: Path, name: str, profile: str):
        path = workspace.resolve()
        trusted = oracle.resolve()
        if path == self.root or self.root not in path.parents or workspace.is_symlink():
            raise ValueError("Workspace must be an isolated child of the sandbox root")
        if (
            trusted == path
            or path in trusted.parents
            or trusted in path.parents
            or oracle.is_symlink()
        ):
            raise ValueError("Oracle must be independent of the candidate workspace")
        if not path.is_dir() or not trusted.is_dir():
            raise ValueError("Workspace and oracle must exist")
        if any(item.is_symlink() for directory in (path, trusted) for item in directory.rglob("*")):
            raise ValueError("Sandbox inputs must not contain symlinks")
        if any("," in str(p) for p in (path, trusted)):
            raise ValueError("Mount paths cannot contain commas")
        if profile != "python":
            raise ValueError("Unknown verification profile")
        # Import trusted pytest before adding candidate modules. Ignore repository config,
        # conftest and installed third-party plugin auto-loading.
        bootstrap = (
            "import sys, pytest; sys.path.insert(0, '/workspace/src'); "
            "raise SystemExit(pytest.main(['-q', '-c', '/dev/null', '--noconftest', "
            "'--confcutdir=/oracle', '-p', 'no:cacheprovider', '/oracle']))"
        )
        return [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--log-driver=none",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            "--cpus=2",
            "--memory=2g",
            "--memory-swap=2g",
            "--user=10000:10000",
            "--shm-size=16m",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=128m",
            "--mount",
            f"type=bind,src={path},dst=/workspace,readonly",
            "--mount",
            f"type=bind,src={trusted},dst=/oracle,readonly",
            "--workdir=/oracle",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            self.image,
            "python",
            "-I",
            "-B",
            "-c",
            bootstrap,
        ]

    def verify(
        self,
        workspace: Path,
        profile: str = "python",
        *,
        oracle: Path | None = None,
        timeout_seconds: int = 600,
        output_limit: int = 128_000,
    ) -> SandboxResult:
        path = workspace.resolve()
        if path == self.root or self.root not in path.parents or workspace.is_symlink():
            raise ValueError("Workspace must be an isolated child of the sandbox root")
        if oracle is None:
            raise ValueError("An independent operator-owned oracle is required")
        if not 1 <= timeout_seconds <= 600 or not 1 <= output_limit <= 128_000:
            raise ValueError("Invalid sandbox limits")
        name = "agent-verify-" + uuid.uuid4().hex
        args = self.command(workspace, oracle, name, profile)
        # Drain a pipe under a byte/deadline cap: no unbounded host temp files or Docker logs.
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output, limit = bytearray(), None
        deadline = time.monotonic() + timeout_seconds
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        limit = "TIMEOUT"
                        break
                    if not selector.select(min(remaining, 0.2)):
                        continue
                    chunk = os.read(
                        process.stdout.fileno(), min(8192, output_limit - len(output) + 1)
                    )
                    if not chunk:
                        try:
                            process.wait(timeout=max(0.001, deadline - time.monotonic()))
                        except subprocess.TimeoutExpired:
                            limit = "TIMEOUT"
                        break
                    output.extend(chunk)
                    if len(output) > output_limit:
                        del output[output_limit:]
                        limit = "OUTPUT_LIMIT"
                        break
        finally:
            if limit or process.poll() is None:
                try:
                    subprocess.run(
                        ["docker", "kill", name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                        check=False,
                    )
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
            process.stdout.close()
        return SandboxResult(process.returncode, output.decode(errors="replace"), limit)
