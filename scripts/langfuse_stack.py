"""Offline preparation of the isolated, local-only Langfuse Compose stack."""

import argparse
import json
import os
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "ops/langfuse"
SECRET_KEYS = (
    "POSTGRES_PASSWORD",
    "CLICKHOUSE_PASSWORD",
    "REDIS_AUTH",
    "MINIO_ROOT_PASSWORD",
    "SALT",
    "ENCRYPTION_KEY",
    "NEXTAUTH_SECRET",
    "LANGFUSE_INIT_USER_PASSWORD",
)
INIT_KEYS = (
    "LANGFUSE_INIT_ORG_ID",
    "LANGFUSE_INIT_PROJECT_ID",
    "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
    "LANGFUSE_INIT_PROJECT_SECRET_KEY",
    "LANGFUSE_INIT_USER_EMAIL",
)
REQUIRED_KEYS = set(SECRET_KEYS + INIT_KEYS)
REFERENCE = re.compile(r"\$\{([A-Z_]+):\?[^}]+\}")


def check(compose: dict, lock: dict) -> None:
    """Repository policy checks; not a replacement for Docker Compose validation."""
    if lock.get("schema_version") != 1:
        raise ValueError("unsupported image lock schema")
    services = compose["services"]
    if set(services) != {"web", "worker", "postgres", "redis", "clickhouse", "minio"}:
        raise ValueError("unexpected services")
    if set(lock["images"]) != set(services):
        raise ValueError("incomplete image lock")
    if compose.get("name") != "agent-py-langfuse-local":
        raise ValueError("stack must have an independent project name")
    if set(REFERENCE.findall(json.dumps(compose))) != REQUIRED_KEYS:
        raise ValueError("required credential references differ from initializer")
    for name, service in services.items():
        image = lock["images"][name]
        if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image["image"]):
            raise ValueError("image digest required")
        if service.get("image") != image["image"]:
            raise ValueError("compose image differs from lock")
        platforms = {(p["os"], p["architecture"]) for p in image["platforms"]}
        if not {("linux", "amd64"), ("linux", "arm64")} <= platforms:
            raise ValueError("missing supported image platforms")
        if name in {"web", "worker"}:
            version = ":" + lock["langfuse_version"] + "@"
            if version not in image["image"]:
                raise ValueError("web and worker version mismatch")
            dependencies = service.get("depends_on", {})
            if dependencies != {
                k: {"condition": "service_healthy"}
                for k in ("postgres", "redis", "clickhouse", "minio")
            }:
                raise ValueError("healthy infrastructure dependencies required")
        expected_ports = {
            "web": ["127.0.0.1:3000:3000"],
            "minio": ["127.0.0.1:9090:9000"],
        }.get(name, [])
        if service.get("ports", []) != expected_ports:
            raise ValueError("unexpected published port")
        if any(k in service for k in ("network_mode", "privileged", "build", "container_name")):
            raise ValueError("unexpected isolation override")
        for volume in service.get("volumes", []):
            if volume.split(":")[0] not in compose["volumes"]:
                raise ValueError("only project-owned named volumes allowed")
    if any(v.get("external") or "name" in v for v in compose["volumes"].values()):
        raise ValueError("shared volumes forbidden")


def read_configuration() -> tuple[dict, dict]:
    compose = json.loads((STACK / "compose.json").read_text())
    lock = json.loads((STACK / "images.lock.json").read_text())
    check(compose, lock)
    return compose, lock


def write_private(path: Path, values: dict[str, str]) -> None:
    # O_EXCL also rejects existing symlinks; creation permissions never expose secrets.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))


def initialize(directory: Path, tenant: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", tenant):
        raise ValueError("tenant must be 1-64 ASCII letters, digits, underscores or hyphens")
    read_configuration()
    values = {key: secrets.token_hex(32) for key in SECRET_KEYS}
    values.update(
        LANGFUSE_INIT_ORG_ID="agent-org-" + secrets.token_hex(12),
        LANGFUSE_INIT_PROJECT_ID="agent-project-" + secrets.token_hex(12),
        LANGFUSE_INIT_PROJECT_PUBLIC_KEY="pk-lf-" + secrets.token_hex(16),
        LANGFUSE_INIT_PROJECT_SECRET_KEY="sk-lf-" + secrets.token_hex(32),
        LANGFUSE_INIT_USER_EMAIL="operator@agent.local",
    )
    # Existing directories are never reused: salt/encryption keys must survive restarts.
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_private(directory / "stack.env", values)
    write_private(
        directory / "agent.env",
        {
            "AGENT_ENVIRONMENT": "development",
            "AGENT_LANGFUSE_ENABLED": "true",
            "AGENT_LANGFUSE_BASE_URL": "http://localhost:3000",
            "AGENT_LANGFUSE_TENANT": tenant,
            "AGENT_LANGFUSE_PUBLIC_KEY": values["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"],
            "AGENT_LANGFUSE_SECRET_KEY": values["LANGFUSE_INIT_PROJECT_SECRET_KEY"],
            "AGENT_LANGFUSE_PSEUDONYM_KEY": secrets.token_hex(32),
            "AGENT_LANGFUSE_SAMPLE_RATE": "1",
            "LANGFUSE_EXPECTED_PROJECT_ID": values["LANGFUSE_INIT_PROJECT_ID"],
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="offline repository policy validation; does not contact Docker")
    init = sub.add_parser("init", help="create new private credentials; never overwrite")
    init.add_argument("--directory", type=Path, default=ROOT / ".runtime/langfuse-selfhost")
    init.add_argument("--tenant", default="demo")
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize(args.directory, args.tenant)
            print("Created private stack.env and agent.env; deployment NOT_RUN")
        else:
            read_configuration()
            print("Static policy PASS; Docker validation and deployment NOT_RUN")
    except (OSError, ValueError, KeyError, TypeError):
        # Do not echo arbitrary path contents, interpolated configuration, or credentials.
        parser.exit(1, "Stack preparation failed; check configuration and use a new directory.\n")


if __name__ == "__main__":
    main()
