import httpx
import pytest

from agent_py.adapters.enterprise import EnterpriseClient
from agent_py.domain import DomainError, digest
from agent_py.model import AnthropicGateway
from agent_py.sandbox import DockerSandbox


def test_enterprise_reader_does_not_follow_credentials_to_other_origin():
    def handler(request):
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(302, headers={"location": "https://attacker.invalid/"})

    client = EnterpriseClient(
        "https://example.test/api/", "secret", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(DomainError, match="Cross-origin"):
        client.get("https://attacker.invalid/")
    with pytest.raises(DomainError, match="Redirect"):
        client.get("test")
    client.close()


def test_enterprise_output_limit():
    client = EnterpriseClient(
        "https://example.test/",
        "secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b" " * 2_000_001)),
    )
    with pytest.raises(DomainError, match="exceeds"):
        client.get("oversized")
    client.close()


def test_model_gateway_parses_native_tool_decision_and_charges(env, task):
    service, p, _ = env
    service.settings.model_id = "configured-test-model"
    service.settings.model_api_key = "unused-test-key"  # validated SecretStr set below
    from pydantic import SecretStr

    service.settings.model_api_key = SecretStr("unused-test-key")

    def handler(request):
        return httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 30, "output_tokens": 10},
                "content": [
                    {
                        "type": "tool_use",
                        "name": "submit_decision",
                        "input": {
                            "summary": "Investigate queue",
                            "hypotheses": ["capacity"],
                            "evidence_ids": ["e1"],
                            "stop": True,
                        },
                    }
                ],
            },
        )

    gateway = AnthropicGateway(service.settings, service, 1, 2, httpx.MockTransport(handler))
    decision = gateway.decide("t1", task.id, "llm1", "Investigate", {"documents": [{"id": "e1"}]})
    assert decision.stop
    assert service.get_task(p, task.id).spent == 50


def test_sandbox_rejects_unpinned_images_and_path_escape(tmp_path):
    with pytest.raises(ValueError, match="digest"):
        DockerSandbox("python:latest", tmp_path)
    sandbox = DockerSandbox("python@sha256:" + digest("image"), tmp_path)
    with pytest.raises(ValueError, match="isolated"):
        sandbox.verify(tmp_path.parent)
