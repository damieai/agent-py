"""Fresh-process Activity fixture; all model/export HTTP uses MockTransport."""

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import httpx
from pydantic import SecretStr

from agent_py import langfuse_export
from agent_py.adapters.simulation import DisabledLiveExecutor
from agent_py.config import Settings
from agent_py.db import Database
from agent_py.investigation import InvestigationHarness
from agent_py.model import AnthropicGateway
from agent_py.runtime import Activities
from agent_py.service import Service


def main():
    if os.environ.get("AGENT_TRACE_FIXTURE") != "1":
        raise SystemExit("Explicit trace fixture opt-in required")
    for name in list(os.environ):
        if name.startswith("AGENT_"):
            del os.environ[name]
    config = json.loads(Path(sys.argv[1]).read_text())
    root = Path(config["root"])
    exported, paid = [], []
    original = langfuse_export.LangfuseProcessor

    def capture(request):
        exported.append(base64.b64encode(request.content).decode())
        return httpx.Response(200)

    langfuse_export.LangfuseProcessor = lambda s, c: original(s, c, httpx.MockTransport(capture))
    settings = Settings(
        environment="test",
        execution_mode="live",
        database_url=f"sqlite:///{root / 'app.db'}",
        artifact_root=root / "artifacts",
        allow_model_api=True,
        model_id="test-model",
        model_api_key=SecretStr("test-only"),
        model_input_micro_per_token=1,
        model_output_micro_per_token=2,
        langfuse_enabled=True,
        langfuse_tenant="t1",
        langfuse_base_url="https://langfuse.example",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        langfuse_pseudonym_key="pseudonym-test-key-" * 3,
        _env_file=None,
    )
    service = Service(Database(settings.database_url), settings, DisabledLiveExecutor())

    def respond(request):
        paid.append(1)
        context = json.loads(json.loads(request.content)["messages"][0]["content"])["context"]
        last = context["round"] == 2
        return httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 30, "output_tokens": 10},
                "content": [
                    {
                        "type": "tool_use",
                        "name": "submit_investigation",
                        "input": {
                            "summary": "Fixture analysis",
                            "hypotheses": [],
                            "evidence_ids": ["beta" if last else "alpha"],
                            "stop": last,
                            "next_query": None if last else "beta",
                        },
                    }
                ],
            },
        )

    try:
        activities = Activities(service)
        activities.harness = InvestigationHarness(
            service,
            AnthropicGateway(
                settings,
                service,
                1,
                2,
                httpx.MockTransport(respond),
            ),
        )
        result = asyncio.run(activities.tick(config["identity"]))
    finally:
        service.telemetry.close()
        service.db.engine.dispose()
    Path(config["output"]).write_text(
        json.dumps({"result": result, "paid": len(paid), "spans": exported})
    )


if __name__ == "__main__":
    main()
