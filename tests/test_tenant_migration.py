"""Upgrade populated legacy databases; reject corruption without partial DDL."""

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import BatchOperations
from sqlalchemy import inspect, text
from test_tenant_integrity import REFERENCES, reject_reference, seed_graph

from agent_py.adapters.simulation import SimulatedSystem
from agent_py.config import Settings, get_settings
from agent_py.db import Database, Outbox, uid
from agent_py.domain import Principal
from agent_py.service import Service


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/legacy.db"
    monkeypatch.setenv("AGENT_DATABASE_URL", url)
    get_settings.cache_clear()
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0008_dependency_circuits")
    db = Database(url)
    settings = Settings(database_url=url, artifact_root=tmp_path / "artifacts", _env_file=None)
    service = Service(db, settings, SimulatedSystem(tmp_path / "remote.db"))
    from agent_py.db import Grant

    for tenant in ("t1", "t2"):
        with db.session(tenant) as s:
            s.add(Grant(tenant_id=tenant, subject="actor", project="demo", environment="lab"))
    principal = Principal(
        tenant_id="t1",
        subject="actor",
        roles=["developer"],
        projects=["demo"],
        environments=["lab"],
    )
    graphs = [
        seed_graph(service, principal.model_copy(update={"tenant_id": tenant}))
        for tenant in ("t1", "t2")
    ]
    yield cfg, db, graphs
    db.engine.dispose()
    get_settings.cache_clear()


def assert_legacy(db):
    with db.engine.connect() as c:
        assert (
            c.scalar(text("SELECT version_num FROM alembic_version")) == "0008_dependency_circuits"
        )
        assert c.scalar(text("SELECT count(*) FROM tasks")) == 2
    assert not inspect(db.engine).get_foreign_keys("operations")
    assert "uq_tasks_tenant_id_id" not in {
        c["name"] for c in inspect(db.engine).get_unique_constraints("tasks")
    }


def test_populated_upgrade_downgrade_reupgrade_preserves_rows_and_constraints(legacy):
    cfg, db, graphs = legacy
    with db.engine.connect() as c:
        before = {
            name: c.exec_driver_sql(f"SELECT * FROM {name} ORDER BY id").all()
            for name in inspect(db.engine).get_table_names()
            if name != "alembic_version"
        }
    command.upgrade(cfg, "head")
    command.check(cfg)
    for reference in REFERENCES:
        reject_reference(db, "t1", graphs[0], reference, graphs[1][reference[2]])
    with db.engine.connect() as c:
        for name, rows in before.items():
            assert c.exec_driver_sql(f"SELECT * FROM {name} ORDER BY id").all() == rows
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    command.downgrade(cfg, "0008_dependency_circuits")
    assert_legacy(db)
    command.upgrade(cfg, "head")
    command.check(cfg)


@pytest.mark.parametrize("invalid", ["missing", "cross_tenant"])
def test_legacy_corruption_rejected_without_partial_schema_or_data_changes(legacy, invalid):
    cfg, db, graphs = legacy
    with db.session("t1") as s:
        s.add(Outbox(tenant_id="t1", task_id=uid() if invalid == "missing" else graphs[1]["tasks"]))
    with pytest.raises(RuntimeError, match="outbox.task_id: 1"):
        command.upgrade(cfg, "head")
    assert_legacy(db)
    with db.engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM outbox")) == 3


def test_mid_migration_failure_rolls_back_sqlite_table_rebuilds(legacy, monkeypatch):
    cfg, db, _ = legacy
    original = BatchOperations.create_foreign_key

    def fail(self, name, *args, **kwargs):
        if name == "fk_artifacts_task_id_tenant":
            raise RuntimeError("injected after parent rebuild")
        return original(self, name, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(BatchOperations, "create_foreign_key", fail)
        with pytest.raises(RuntimeError, match="injected"):
            command.upgrade(cfg, "head")
    assert_legacy(db)
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_existing_circuit_preserved_and_bulkhead_limit_initialized_after_upgrade(legacy):
    from agent_py.db import DependencyCircuit
    from agent_py.resilience import acquire, complete, dependency_status, set_read_limit

    cfg, db, _ = legacy
    command.upgrade(cfg, "0009_tenant_references")
    with db.engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO dependency_circuits (id, tenant_id, created_at, dependency, provider, revision, generation, failures, state) VALUES (:id, 't1', CURRENT_TIMESTAMP, 'existing', 'jenkins_build', 3, 7, 2, 'CLOSED')"
            ),
            {"id": uid()},
        )
    command.upgrade(cfg, "head")
    with db.session("t1") as s:
        row = s.query(DependencyCircuit).one()
        assert (row.generation, row.failures, row.max_in_flight) == (7, 2, 0)
    service = Service(db, Settings(max_reads_per_dependency=3, _env_file=None), None)
    permit = acquire(service, "t1", "existing", "jenkins_build")
    assert dependency_status(service, "t1")[0]["read_limit"] == 3
    set_read_limit(service, "t1", "existing", 2)
    complete(service, permit, None)
    command.downgrade(cfg, "0009_tenant_references")
    assert "dependency_read_leases" not in inspect(db.engine).get_table_names()
    command.upgrade(cfg, "head")
    command.check(cfg)
    with db.session("t1") as s:
        row = s.query(DependencyCircuit).one()
        assert (row.generation, row.failures, row.max_in_flight) == (7, 2, 0)
