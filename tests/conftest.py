import pytest

from agent_py.adapters.simulation import SimulatedSystem
from agent_py.config import Settings
from agent_py.db import Database, Grant
from agent_py.domain import Principal, TaskContract
from agent_py.service import Service


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{tmp_path}/app.db",
        artifact_root=tmp_path / "artifacts",
        auth_secret="unit-test-secret-" * 4,
        _env_file=None,
    )
    db = Database(settings.database_url)
    db.create_schema()
    remote = SimulatedSystem(tmp_path / "remote.db")
    service = Service(db, settings, remote)
    p = Principal(
        tenant_id="t1",
        subject="u1",
        roles=["developer", "operator"],
        projects=["demo"],
        environments=["lab"],
    )
    reviewer = p.model_copy(update={"subject": "reviewer", "roles": ["approver", "operator"]})
    for tenant in ["t1", "t2"]:
        with db.session(tenant) as s:
            for subject in ["u1", "reviewer"]:
                s.add(Grant(tenant_id=tenant, subject=subject, project="demo", environment="lab"))
    yield service, p, reviewer
    db.engine.dispose()


@pytest.fixture
def task(env):
    service, p, _ = env
    return service.create_task(
        p, TaskContract(kind="repair", goal="Fix a regression", project="demo"), "request-1"
    )
