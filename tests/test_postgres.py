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
    from test_tenant_integrity import REFERENCES, reject_reference, seed_graph

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
        graphs = {
            tenant: seed_graph(service, p.model_copy(update={"tenant_id": tenant}))
            for tenant in ("one", "two")
        }
        # Rehearse a populated PostgreSQL rollback/upgrade; invalid legacy data must
        # leave the schema revision unchanged, including under forced tenant RLS.
        command.downgrade(Config("alembic.ini"), "0008_dependency_circuits")
        from sqlalchemy.exc import IntegrityError

        from agent_py.db import Outbox, uid

        bad_id = uid()
        with engine.begin() as c:
            c.execute(
                Outbox.__table__.insert().values(
                    id=bad_id, tenant_id="one", task_id=graphs["two"]["tasks"], delivered=False
                )
            )
        with pytest.raises(RuntimeError, match="outbox.task_id: 1"):
            command.upgrade(Config("alembic.ini"), "head")
        with engine.begin() as c:
            assert (
                c.scalar(text("SELECT version_num FROM alembic_version"))
                == "0008_dependency_circuits"
            )
            c.execute(text("DELETE FROM outbox WHERE id=:id"), {"id": bad_id})
        command.upgrade(Config("alembic.ini"), "head")
        command.check(Config("alembic.ini"))
        # A downgrade drops the new lease table; re-created tables need DML grants again.
        with engine.begin() as c:
            c.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO agent_test_app"
                )
            )
        # Even the migration administrator cannot commit an invalid reference.
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(
                    Outbox.__table__.insert().values(
                        tenant_id="one", task_id=graphs["two"]["tasks"], delivered=False
                    )
                )
        for reference in REFERENCES:
            reject_reference(appdb, "one", graphs["one"], reference, graphs["two"][reference[2]])
            reject_reference(appdb, "one", graphs["one"], reference, "missing")
        # Seeded fixtures have a work lease; remove them before admission counting below.
        with engine.begin() as c:
            c.execute(text("DELETE FROM work_leases"))
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
        from agent_py.context import ContextCompiler
        from agent_py.db import Document

        for tenant in ("one", "two"):
            with appdb.session(tenant) as s:
                s.add(
                    Document(
                        tenant_id=tenant,
                        project="demo",
                        source="repo://demo/queue.py",
                        version="v1",
                        body="def worker_capacity():\n    return 8\n",
                        allowed_subjects=["actor"],
                    )
                )
        compiler = ContextCompiler(appdb, "bm25_rrf")
        bundle = compiler.compile(p, "demo", "lab", "worker capacity", task_id=task.id)
        assert len(bundle.documents) == 1
        assert bundle.documents[0]["symbol"] == "worker_capacity"
        compiler.validate(p, "demo", "lab", bundle, task_id=task.id)
        from agent_py.audit import check_recording, export_audit
        from agent_py.db import Task

        with appdb.snapshot("one") as s:
            before = s.get(Task, task.id).next_sequence
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(service.stop, p, task.id).result()
            # Reads remain on the original MVCC snapshot despite the committed cancellation.
            assert (
                s.execute(
                    text("SELECT max(sequence) FROM task_events WHERE task_id=:id"), {"id": task.id}
                ).scalar()
                == before
            )
            assert s.execute(text("SELECT DISTINCT tenant_id FROM tasks")).scalars().all() == [
                "one"
            ]
        audit = export_audit(service, p, task.id)
        assert check_recording(audit)["event_count"] == before + 1
        from agent_py.resilience import acquire as acquire_read
        from agent_py.resilience import complete as complete_read

        for _ in range(3):
            complete_read(
                service, acquire_read(service, "one", "fixture", "jenkins_build"), "failure"
            )
        with pytest.raises(DomainError, match="cooling"):
            acquire_read(service, "one", "fixture", "jenkins_build")
        assert acquire_read(service, "two", "fixture", "jenkins_build")
        with appdb.session("one") as s:
            assert s.execute(text("SELECT tenant_id FROM dependency_circuits")).scalars().all() == [
                "one"
            ]
            s.execute(text("UPDATE dependency_circuits SET retry_at = now() - interval '1 second'"))

        def probe(_):
            try:
                return acquire_read(service, "one", "fixture", "jenkins_build")
            except DomainError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            probes = [p for p in pool.map(probe, range(4)) if p]
        assert len(probes) == 1 and probes[0].probe_token
        complete_read(service, probes[0], "success")
        # Real pooled connections must share a single healthy-dependency capacity.
        from agent_py.db import DependencyReadLease, now
        from agent_py.resilience import dependency_status, set_read_limit

        initial = acquire_read(service, "one", "bulkhead", "jenkins_build")
        complete_read(service, initial, "success")
        set_read_limit(service, "one", "bulkhead", 2)

        def read_slot(_):
            try:
                return acquire_read(service, "one", "bulkhead", "jenkins_build")
            except DomainError as exc:
                assert exc.code == "DEPENDENCY_CAPACITY"
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            slots = [permit for permit in pool.map(read_slot, range(4)) if permit]
        assert len(slots) == 2
        assert acquire_read(service, "two", "foreign-only", "jenkins_build")
        with appdb.session("one") as s:
            assert s.execute(
                text("SELECT DISTINCT tenant_id FROM dependency_read_leases")
            ).scalars().all() == ["one"]
        with appdb.engine.connect() as c:
            assert c.scalar(text("SELECT count(*) FROM dependency_read_leases")) == 0
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(
                    DependencyReadLease.__table__.insert().values(
                        tenant_id="one", dependency="foreign-only", expires_at=now()
                    )
                )
        complete_read(service, slots[0], "success")
        assert acquire_read(service, "one", "bulkhead", "jenkins_build")
        status = next(
            row for row in dependency_status(service, "one") if row["dependency"] == "bulkhead"
        )
        assert status["active_reads"] == status["read_limit"] == 2
    finally:
        get_settings.cache_clear()
        if appdb:
            appdb.engine.dispose()
        if engine:
            engine.dispose()
        pg.cleanup()
