import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    output: str


class DockerSandbox:
    """No host execution fallback. Only operator-defined verification profiles are accepted."""

    def __init__(self, image: str, root: Path):
        if "@sha256:" not in image:
            raise ValueError("Sandbox image must be pinned by digest")
        self.image, self.root = image, root.resolve()

    def verify(self, workspace: Path, profile: str = "python") -> SandboxResult:
        path = workspace.resolve()
        if path == self.root or self.root not in path.parents or workspace.is_symlink():
            raise ValueError("Workspace must be an isolated child of the sandbox root")
        commands = {"python": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]}
        if profile not in commands:
            raise ValueError("Unknown verification profile")
        import uuid

        name = "agent-verify-" + uuid.uuid4().hex
        args = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            "--cpus=2",
            "--memory=2g",
            "--user=10000:10000",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=128m",
            "--mount",
            f"type=bind,src={path},dst=/workspace,readonly",
            "--workdir=/workspace",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            self.image,
            *commands[profile],
        ]
        # Write output to a bounded tmpfs in container in a future sandbox protocol; this
        # adapter caps returned output but logs may occupy host temp space during execution.
        import tempfile

        try:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(
                    args, stdout=output, stderr=subprocess.STDOUT, timeout=600, check=False
                )
                output.seek(0)
                return SandboxResult(
                    result.returncode, output.read(128_000).decode(errors="replace")
                )
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=15, check=False)
            raise
