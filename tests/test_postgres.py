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
    from agent_py.db import Database, Grant
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

        def reserve(i):
            try:
                service.reserve("one", task.id, f"call-{i}", 600_000)
                return True
            except DomainError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(reserve, [1, 2])) == 1
    finally:
        get_settings.cache_clear()
        if appdb:
            appdb.engine.dispose()
        if engine:
            engine.dispose()
        pg.cleanup()
