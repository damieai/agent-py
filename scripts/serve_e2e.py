"""Loopback-only disposable browser fixture. Never included in the production app."""

import os
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from fastapi import Header, HTTPException
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from agent_py.adapters.simulation import SimulatedSystem
from agent_py.api import create_app
from agent_py.config import Settings
from agent_py.db import Database, Document, Grant, uid
from agent_py.domain import Principal
from agent_py.runtime import Activities
from agent_py.security import issue_dev_token


def main():
    control_key = os.environ.get("E2E_CONTROL_KEY", "")
    if os.environ.get("E2E_FIXTURE") != "1" or len(control_key) < 32:
        raise SystemExit(
            "Use the Playwright runner; explicit fixture opt-in and random key required"
        )
    # This is a separate subprocess: never inherit developer/production Agent configuration.
    for name in list(os.environ):
        if name.startswith("AGENT_"):
            del os.environ[name]
    with TemporaryDirectory(prefix="agent-browser-") as temporary:
        root = Path(temporary)
        settings = Settings(
            environment="test",
            execution_mode="simulation",
            database_url=f"sqlite:///{root}/app.db",
            artifact_root=root / "artifacts",
            auth_secret=secrets.token_urlsafe(48),
            _env_file=None,
        )
        db = Database(settings.database_url)
        db.create_schema()
        app = create_app(settings, db, SimulatedSystem(root / "remote.db"))
        service = app.state.service
        sessions = {}

        def authorize(key):
            if not secrets.compare_digest(key, control_key):
                raise HTTPException(403, "Fixture control denied")

        @app.post("/__test/session")
        def session(x_e2e_key: str = Header(default="")):
            authorize(x_e2e_key)
            tenant = uid()
            principals = {
                name: Principal(
                    tenant_id=tenant,
                    subject=name,
                    roles=["approver", "operator"]
                    if name == "reviewer"
                    else ["developer", "operator"],
                    projects=["demo"],
                    environments=["lab"],
                )
                for name in ("owner", "reviewer", "outsider")
            }
            with db.session(tenant) as s:
                for name in ("owner", "reviewer"):
                    s.add(Grant(tenant_id=tenant, subject=name, project="demo", environment="lab"))
                s.add(
                    Document(
                        tenant_id=tenant,
                        project="demo",
                        source="fixture://browser/evidence",
                        version="1",
                        body="BROWSER_EVIDENCE: queue capacity investigation",
                        allowed_subjects=["owner", "reviewer"],
                    )
                )
            sessions[tenant] = principals
            return {
                "id": tenant,
                **{name: issue_dev_token(settings, p) for name, p in principals.items()},
            }

        @app.post("/__test/{identity}/tick/{task_id}")
        async def tick(identity: str, task_id: str, x_e2e_key: str = Header(default="")):
            authorize(x_e2e_key)
            if identity not in sessions:
                raise HTTPException(404)
            service.get_task(sessions[identity]["owner"], task_id)
            return await Activities(service).tick({"tenant": identity, "task_id": task_id})

        @app.post("/__test/{identity}/revoke")
        def revoke(identity: str, x_e2e_key: str = Header(default="")):
            authorize(x_e2e_key)
            if identity not in sessions:
                raise HTTPException(404)
            with db.session(identity) as s:
                for grant in s.scalars(select(Grant).where(Grant.subject == "owner")):
                    grant.revoked = True
            return {"revoked": True}

        app.mount(
            "/", StaticFiles(directory=Path(__file__).resolve().parents[1] / "web/dist", html=True)
        )
        try:

            class ReadyServer(uvicorn.Server):
                async def startup(self, sockets=None):
                    await super().startup(sockets)
                    if self.started:
                        print("E2E_SERVER_READY", flush=True)

            ReadyServer(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=18765,
                    log_level="warning",
                    access_log=False,
                )
            ).run()
        finally:
            db.engine.dispose()


if __name__ == "__main__":
    main()
