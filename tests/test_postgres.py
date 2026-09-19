import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("AGENT_TEST_POSTGRES") != "1", reason="Native PostgreSQL opt-in"),
]


def test_postgres_migrations_rls_and_concurrent_budget(tmp_path, monkeypatch):
    import pgserver
    from alembic import command
    from alembic.config import Config

    from agent_py.adapters.simulation import SimulatedSystem
    from agent_py.config import Settings, get_settings
    from agent_py.db import Database, Grant, RepairAttempt, RepairRun
    from agent_py.domain import DomainError, Principal, TaskContract
    from agent_py.service import Service

    pg = pgserver.get_server(tmp_path / "pgdata", cleanup_mode="stop")
    engine = None
    appdb = None
    try:
        url = pg.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        monkeypatch.setenv("AGENT_DATABASE_URL", url)
        get_settings.cache_clear()
        command.upgrade(Config("alembic.ini"), "head")
        engine = create_engine(url)
        with engine.begin() as c:
            c.execute(text("CREATE ROLE agent_test_app LOGIN NOSUPERUSER NOBYPASSRLS"))
            c.execute(text("GRANT USAGE ON SCHEMA public TO agent_test_app"))
            c.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO agent_test_app"
                )
            )
        # Every pooled connection uses the unprivileged role, including raw SELECTs.
        appdb = Database(url)
        from sqlalchemy import event

        @event.listens_for(appdb.engine, "connect")
        def use_role(connection, record):
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE agent_test_app")
            connection.commit()

        for tenant in ["one", "two"]:
            with appdb.session(tenant) as s:
                s.add(Grant(tenant_id=tenant, subject="actor", project="demo", environment="lab"))
        appdb.assert_production_role()
        with appdb.session("one") as s:
            assert s.execute(text("SELECT tenant_id FROM grants")).scalars().all() == ["one"]
        with appdb.engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM grants")).scalar() == 0
        settings = Settings(database_url=url, artifact_root=tmp_path / "artifacts", _env_file=None)
        service = Service(appdb, settings, SimulatedSystem(tmp_path / "remote.db"))
        p = Principal(
            tenant_id="one",
            subject="actor",
            roles=["developer"],
            projects=["demo"],
            environments=["lab"],
        )
        task = service.create_task(
            p, TaskContract(kind="repair", project="demo", goal="Test concurrency"), "key"
        )
        for tenant in ("one", "two"):
            with appdb.session(tenant) as s:
                run = RepairRun(
                    tenant_id=tenant,
                    task_id=task.id,
                    snapshot_id="fixture",
                    source_digest="a" * 64,
                    config_digest="b" * 64,
                    max_attempts=2,
                )
                s.add(run)
                s.flush()
                s.add(RepairAttempt(tenant_id=tenant, run_id=run.id, ordinal=1))
        with appdb.session("one") as s:
            assert s.execute(text("SELECT tenant_id FROM repair_runs")).scalars().all() == ["one"]
            assert s.execute(text("SELECT tenant_id FROM repair_attempts")).scalars().all() == [
                "one"
            ]
        with appdb.engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM repair_runs")).scalar() == 0
            assert c.execute(text("SELECT count(*) FROM repair_attempts")).scalar() == 0

        def reserve(i):
            try:
                service.reserve("one", task.id, f"call-{i}", 600_000)
                return True
            except DomainError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(reserve, [1, 2])) == 1
        from agent_py.operations import summary
        from agent_py.scheduling import acquire

        service.settings.max_active_per_tenant = 1
        jobs = [
            service.create_task(
                p,
                TaskContract(kind="repair", project="demo", goal="Concurrent worker admission"),
                f"job-{i}",
            )
            for i in range(4)
        ]
        with ThreadPoolExecutor(max_workers=4) as pool:
            admissions = list(pool.map(lambda job: acquire(service, "one", job.id), jobs))
        assert sum(bool(token) for token, _ in admissions) == 1
        other = service.create_task(
            p.model_copy(update={"tenant_id": "two"}),
            TaskContract(kind="repair", project="demo", goal="Other tenant capacity"),
            "other",
        )
        assert acquire(service, "two", other.id)[0]
        with appdb.session("one") as s:
            assert s.execute(
                text("SELECT DISTINCT tenant_id FROM work_leases")
            ).scalars().all() == ["one"]
            assert s.execute(text("SELECT tenant_id FROM admission_gates")).scalars().all() == [
                "one"
            ]
        snapshot = summary(service, "one", p.model_copy(update={"roles": ["operator"]}))
        assert snapshot["admission"]["active"] == 1
        assert snapshot["admission"]["waiting"] == 3
        assert snapshot["today_reserved_micro_usd"] == 600_000
    finally:
        get_settings.cache_clear()
        if appdb:
            appdb.engine.dispose()
        if engine:
            engine.dispose()
        pg.cleanup()
