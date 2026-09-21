"""Deployment guardrails and credential persistence, without Docker or network."""

import hashlib
import importlib
import json
import stat
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from agent_py.config import Settings


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("langfuse_stack")


def test_bootstrap_private_matching_credentials_and_no_rotation(stack, tmp_path):
    directory = tmp_path / "credentials"
    stack.initialize(directory, "tenant-a")
    server = dotenv_values(directory / "stack.env")
    agent = dotenv_values(directory / "agent.env")
    assert set(server) == stack.REQUIRED_KEYS
    assert server["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"] == agent["AGENT_LANGFUSE_PUBLIC_KEY"]
    assert server["LANGFUSE_INIT_PROJECT_SECRET_KEY"] == agent["AGENT_LANGFUSE_SECRET_KEY"]
    assert server["LANGFUSE_INIT_PROJECT_ID"] == agent["LANGFUSE_EXPECTED_PROJECT_ID"]
    settings = Settings(_env_file=directory / "agent.env")
    assert settings.langfuse_enabled and settings.langfuse_tenant == "tenant-a"
    assert len({server[k] for k in stack.SECRET_KEYS}) == len(stack.SECRET_KEYS)
    assert len(bytes.fromhex(server["ENCRYPTION_KEY"])) == 32
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(FileExistsError):
        stack.initialize(directory, "tenant-a")
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    other = tmp_path / "other"
    stack.initialize(other, "tenant-a")
    assert dotenv_values(other / "stack.env")["SALT"] != server["SALT"]


@pytest.mark.parametrize("tenant", ["bad\nEVIL=true", "$(touch /tmp/evil)", "", "a" * 65])
def test_bootstrap_rejects_env_and_shell_injection(stack, tmp_path, tenant):
    with pytest.raises(ValueError):
        stack.initialize(tmp_path / "new", tenant)
    assert not (tmp_path / "new").exists()


def test_bootstrap_does_not_follow_existing_symlink(stack, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(FileExistsError):
        stack.initialize(link, "demo")
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("mutation", ["digest", "port", "volume", "dependency", "credential"])
def test_static_checks_reject_dangerous_configuration_drift(stack, mutation):
    compose, lock = stack.read_configuration()
    compose = deepcopy(compose)
    if mutation == "digest":
        compose["services"]["web"]["image"] = "langfuse/langfuse:latest"
    elif mutation == "port":
        compose["services"]["postgres"]["ports"] = ["5432:5432"]
    elif mutation == "volume":
        compose["volumes"]["postgres_data"]["external"] = True
    elif mutation == "dependency":
        compose["services"]["worker"]["depends_on"] = {}
    else:
        compose["services"]["web"]["environment"]["NEXTAUTH_SECRET"] = "default-password"
    with pytest.raises(ValueError):
        stack.check(compose, lock)


def test_resolver_checks_manifest_digest_and_auth_endpoint(stack):
    resolver = importlib.import_module("resolve_langfuse_images")
    body = json.dumps(
        {
            "manifests": [
                {"platform": {"os": "linux", "architecture": a}} for a in ("amd64", "arm64")
            ]
        }
    ).encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()

    def success(request):
        if request.url.host == "auth.docker.io":
            return httpx.Response(200, json={"token": "synthetic-token"})
        if "authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": 'Bearer realm="https://auth.docker.io/token",'
                    'service="registry.docker.io",scope="repository:langfuse/langfuse:pull"'
                },
            )
        return httpx.Response(200, content=body, headers={"docker-content-digest": digest})

    reference = "docker.io/langfuse/langfuse:4.38.0@sha256:" + "0" * 64
    with httpx.Client(transport=httpx.MockTransport(success)) as client:
        result = resolver.resolve(client, reference)
    assert result["image"].endswith("@" + digest)
    for response in (
        httpx.Response(200, content=body, headers={"docker-content-digest": "sha256:wrong"}),
        httpx.Response(
            401, headers={"www-authenticate": 'Bearer realm="https://unexpected.invalid/token"'}
        ),
    ):
        with httpx.Client(transport=httpx.MockTransport(lambda r: response)) as client:
            with pytest.raises(ValueError):
                resolver.resolve(client, reference)
