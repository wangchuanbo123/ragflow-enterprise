"""Safe server backup orchestration plus an opt-in isolated Docker restore drill."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import uuid

import pytest

from scripts import server_backup as backup


def bundle(path, members=None):
    path.mkdir(parents=True)
    backup._restrict_private(path, directory=True)
    backup._private_write(path / "database.dump", b"PGDMPsynthetic-custom-archive")
    backup._private_write(path / "env.original", b'JWT_SECRET_KEY="archived-business-secret"\nPOSTGRES_PASSWORD="archived-infrastructure"\n')
    with tarfile.open(path / "documents.tar.gz", "w:gz") as archive:
        for name, value in members or [("history/old.txt", b"original contents")]:
            info = tarfile.TarInfo(name)
            if value is None:
                info.type = tarfile.SYMTYPE
                info.linkname = "/outside"
                archive.addfile(info)
            else:
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
    manifest = {"format_version": 1, "files": {name: {"bytes": (path / name).stat().st_size,
                 "sha256": backup._sha256(path / name)} for name in backup.COMPONENTS}}
    backup._private_write(path / "manifest.json", json.dumps(manifest).encode())
    return path


@pytest.mark.parametrize("name,value", [("../escape", b"x"), ("/absolute", b"x"), ("C:/escape", b"x"), ("link", None)])
def test_restore_rejects_unsafe_archive_before_container_or_service_changes(tmp_path, name, value):
    folder = bundle(tmp_path / "archive", [(name, value)])
    service = backup.ServerBackup(tmp_path)
    service._run = lambda *args, **kwargs: pytest.fail("Unsafe backup must not invoke Docker")
    with pytest.raises(backup.BackupError):
        service.restore(folder)


def test_checksum_failure_is_detected_before_stopping_services(tmp_path):
    folder = bundle(tmp_path / "archive")
    (folder / "database.dump").write_bytes(b"tampered")
    service = backup.ServerBackup(tmp_path)
    service._run = lambda *args, **kwargs: pytest.fail("Corrupt backup must not invoke Docker")
    with pytest.raises(backup.BackupError, match="checksum"):
        service.restore(folder)


def test_business_settings_restore_preserves_local_infrastructure_and_paths():
    archived = {"POSTGRES_PASSWORD": "old-pg", "REDIS_PASSWORD": "old-redis", "APP_PORT": "8000",
                "LOCAL_OBJECT_STORE_DIR": "/old-machine/docs", "JWT_SECRET_KEY": "old-jwt",
                "ANSWER_LLM_API_KEY": "old-model", "CONNECTOR_CREDENTIALS_JSON": "old-registry",
                "VECTOR_STORE_PROVIDER": "chroma"}
    current = {"POSTGRES_PASSWORD": "new-pg", "REDIS_PASSWORD": "new-redis", "APP_PORT": "9912",
               "LOCAL_OBJECT_STORE_DIR": "/app/data/docs", "JWT_SECRET_KEY": "new-jwt",
               "ANSWER_LLM_API_KEY": "new-model", "VECTOR_STORE_PROVIDER": "pgvector"}
    text = backup.restored_environment(current, archived).decode()
    assert '"new-pg"' in text and '"new-redis"' in text and '"9912"' in text
    assert '"/app/data/docs"' in text and '"pgvector"' in text
    assert '"old-jwt"' in text and '"old-model"' in text and '"old-registry"' in text
    assert "old-pg" not in text and "/old-machine" not in text


@pytest.mark.parametrize("entry", ["TABLE other knowledge_bases owner", "TABLE public unrelated_data owner",
                                  "EXTENSION - unrelated_extension owner", "FUNCTION public arbitrary() owner",
                                  "FUNCTION public bump_knowledge_graph_revision(text) owner",
                                  "FUNCTION other bump_knowledge_graph_revision() owner",
                                  "TRIGGER public users graph_revision_insert owner",
                                  "TRIGGER public kg_entities unrelated_trigger owner",
                                  "TRIGGER other kg_entities graph_revision_insert owner",
                                  "SCHEMA - unrelated_schema owner"])
def test_database_archive_cannot_cross_application_schema_boundary(entry):
    toc = ("1; 1259 1 TABLE public knowledge_bases owner\n2; 1259 2 " + entry + "\n").encode()
    with pytest.raises(backup.BackupError):
        backup.validate_database_toc(toc)


def test_database_directory_accepts_supported_legacy_and_current_app_objects():
    backup.validate_database_toc(b"; Archive directory\n1; 1259 1 TABLE public knowledge_bases owner\n"
        b"2; 0 1 TABLE DATA public knowledge_bases owner\n3; 2606 2 FK CONSTRAINT public graph_filter_views graph_filter_views_knowledge_base_id_fkey owner\n"
        b"4; 0 0 EXTENSION - vector owner\n5; 0 0 COMMENT - EXTENSION vector owner\n"
        b"6; 1255 3 FUNCTION public bump_knowledge_graph_revision() owner\n"
        b"7; 2620 4 TRIGGER public kg_entities graph_revision_insert owner\n"
        b"8; 2620 5 TRIGGER public kg_entities graph_revision_update owner\n"
        b"9; 2620 6 TRIGGER public kg_entities graph_revision_delete owner\n"
        b"10; 2620 7 TRIGGER public kg_entities graph_revision_truncated owner\n")


@pytest.mark.parametrize("scope", [
    {"database": "postgres", "schemas": [], "relations": []},
    {"database": "app", "schemas": ["unrelated"], "relations": []},
    {"database": "app", "schemas": [], "relations": [{"name": "unrelated", "kind": "r"}]},
    {"database": "app", "schemas": [], "relations": [{"name": "knowledge_bases", "kind": "v"}]},
])
def test_database_target_must_be_dedicated_application_scope(tmp_path, scope):
    service = backup.ServerBackup(tmp_path)
    service._run = lambda *args, **kwargs: json.dumps(scope).encode()
    with pytest.raises(backup.BackupError):
        service._database_scope()


class FakeBackup(backup.ServerBackup):
    def __init__(self, root):
        super().__init__(root)
        self.calls, self.applied, self.fail_snapshot = [], [], False

    def _run(self, arguments, **kwargs):
        self.calls.append(arguments)
        if arguments[:4] == ["exec", "-T", "postgres", "pg_restore"]:
            return b"1; 1259 123 TABLE public knowledge_bases backup_test\n"
        return b"api\nworker\n" if arguments[0] == "ps" else b""

    def _database_scope(self):
        pass

    def _snapshot(self, output, previous):
        if self.fail_snapshot:
            raise backup.BackupError("injected snapshot error")
        return bundle(Path(output) / ("snapshot-" + uuid.uuid4().hex))

    def _apply(self, folder, *, merged_environment):
        self.applied.append((folder, merged_environment))
        if len(self.applied) == 1:
            raise backup.BackupError("injected restore error")


def test_failed_backup_resumes_only_previously_running_services(tmp_path):
    service = FakeBackup(tmp_path)
    service.fail_snapshot = True
    with pytest.raises(backup.BackupError, match="injected"):
        service.backup(tmp_path / "backups")
    assert service.calls[-2] == ["start", "--wait", "--wait-timeout", "180", "api"]
    assert service.calls[-1] == ["start", "--wait", "--wait-timeout", "180", "worker"]
    assert "scheduler" not in service.calls[-1]


def test_backup_resumes_before_compression_hashing_and_full_validation(tmp_path):
    service = FakeBackup(tmp_path)
    original = service._finalize_snapshot
    def finalize(folder, previous):
        assert service._capture_only is False
        assert service.calls[-1] == ["start", "--wait", "--wait-timeout", "180", "worker"]
        return original(folder, previous)
    service._finalize_snapshot = finalize
    report = service.backup(tmp_path / "backups")
    assert report["backup_seconds"] >= report["maintenance_seconds"] >= 0
    state = json.loads((tmp_path / "data/operations/maintenance.json").read_text())
    assert state["status"] == "idle" and "backup_seconds" in state


def test_restore_failure_automatically_uses_protection_backup_then_recreates_services(tmp_path):
    folder = bundle(tmp_path / "archive")
    backup._private_write(tmp_path / ".env.server", b'POSTGRES_PASSWORD="current-infrastructure"\nJWT_SECRET_KEY="current-business"\n')
    service = FakeBackup(tmp_path)
    with pytest.raises(backup.BackupError, match="previous database"):
        service.restore(folder)
    assert len(service.applied) == 2
    assert service.applied[0][0] == folder
    assert service.applied[1][0].parent == folder.parent / "pre-restore"
    assert service.applied[1][0].is_dir() and folder.is_dir()
    assert service.calls[-2] == ["up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180", "api"]
    assert service.calls[-1] == ["up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180", "worker"]


def test_double_restore_failure_keeps_all_application_services_stopped(tmp_path):
    folder = bundle(tmp_path / "archive")
    backup._private_write(tmp_path / ".env.server", b'POSTGRES_PASSWORD="current-infrastructure"\n')
    service = FakeBackup(tmp_path)

    def fail(*args, **kwargs):
        raise backup.BackupError("injected failure")

    service._apply = fail
    with pytest.raises(backup.BackupError, match="application services remain stopped") as caught:
        service.restore(folder)
    assert "pre-restore" in str(caught.value)
    assert not any(arguments[0] in {"up", "start"} for arguments in service.calls)
    assert ["stop", "--timeout", "90", "scheduler", "worker", "api"] in service.calls
    assert any((folder.parent / "pre-restore").iterdir())


def test_document_helper_stages_safely_replaces_files_and_removes_newer_physical_files(tmp_path):
    folder = bundle(tmp_path / "archive", [("history/old.txt", b"old-history"), ("images/a.png", b"\x89PNGbinary")])
    root = tmp_path / "documents"
    root.mkdir()
    (root / "newer.txt").write_text("must disappear", encoding="utf-8")
    stage = ".server-restore-stage-" + uuid.uuid4().hex
    command = [sys.executable, "-c", backup.DOCUMENT_HELPER]

    def run(mode, data=None, name=stage):
        result = subprocess.run(command + [mode, str(root), name, "100000"], input=data,
                                capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr.decode()
        return result.stdout

    run("stage", (folder / "documents.tar.gz").read_bytes())
    assert (root / "newer.txt").read_text() == "must disappear"
    run("activate")
    assert not (root / "newer.txt").exists()
    assert (root / "history/old.txt").read_bytes() == b"old-history"
    assert (root / "images/a.png").read_bytes() == b"\x89PNGbinary"
    run("cleanup", name=stage.replace("-stage-", "-old-"))
    assert {item.name for item in root.iterdir()} == {"history", "images"}


def test_helper_refuses_symlinks_in_source_volume(tmp_path):
    # Build a linked directory where permitted; no symlink privilege is assumed.
    root = tmp_path / "documents"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private", encoding="utf-8")
    try:
        (root / "link").symlink_to(outside)
    except OSError:
        pytest.skip("This platform does not allow creating test symbolic links")
    result = subprocess.run([sys.executable, "-c", backup.DOCUMENT_HELPER, "archive", str(root), "", "100000"],
                            capture_output=True, timeout=15)
    assert result.returncode != 0
    assert outside.read_text() == "private"


@pytest.mark.skipif(os.getenv("TEST_SERVER_BACKUP_DOCKER") != "1", reason="Set TEST_SERVER_BACKUP_DOCKER=1 for a dedicated Docker restore drill")
def test_real_docker_backup_restore_keeps_pgvector_documents_settings_and_service_state(tmp_path):
    root = tmp_path / "isolated-server"
    (root / "deploy").mkdir(parents=True)
    name = "rag-backup-drill-" + uuid.uuid4().hex[:12]
    config = root / "deploy/compose.yml"
    config.write_text(f"""name: {name}
services:
  postgres:
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_USER: backup_test
      POSTGRES_DB: backup_test
      POSTGRES_PASSWORD: isolated-backup-test
    ports: ["127.0.0.1::5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U backup_test -d backup_test"]
      interval: 1s
      timeout: 2s
      retries: 60
    volumes:
      - postgres:/var/lib/postgresql/data
  api:
    image: python:3.12-alpine
    init: true
    command: ["python", "-c", "import time; from pathlib import Path; time.sleep(2); Path('/tmp/healthy').write_text('ready'); time.sleep(36000)"]
    healthcheck:
      test: ["CMD", "python", "-c", "from pathlib import Path; assert Path('/tmp/healthy').is_file()"]
      interval: 1s
      timeout: 2s
      retries: 30
    env_file: ["../.env.server"]
    volumes: ["documents:/app/data/docs"]
  worker:
    image: python:3.12-alpine
    init: true
    command: ["python", "-c", "import time; time.sleep(36000)"]
    env_file: ["../.env.server"]
    volumes: ["documents:/app/data/docs"]
  scheduler:
    image: python:3.12-alpine
    init: true
    command: ["python", "-c", "import time; time.sleep(36000)"]
    env_file: ["../.env.server"]
    volumes: ["documents:/app/data/docs"]
volumes:
  documents:
  postgres:
""", encoding="utf-8")
    settings = root / ".env.server"
    backup._private_write(settings, b'POSTGRES_PASSWORD="isolated-backup-test"\nJWT_SECRET_KEY="original-business"\nANSWER_LLM_MODEL="original-model"\nAPP_PORT="8080"\n')
    service = backup.ServerBackup(root, timeout=120)
    base = service.base
    engine = None
    assert name.startswith("rag-backup-drill-") and "rag-enterprise" != name

    def docker(arguments, data=None):
        result = subprocess.run(base + arguments, cwd=root, input=data, capture_output=True, timeout=120,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 0, result.stderr.decode()
        return result.stdout

    def sql(statement):
        return docker(["exec", "-T", "postgres", "sh", "-c", 'exec psql -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"'], statement.encode()).decode().strip()

    try:
        docker(["up", "-d", "--wait", "postgres", "api", "worker"])
        from alembic import command
        from alembic.config import Config
        from sqlalchemy.engine import URL
        from app.core.database import create_database_engine
        port = int(docker(["port", "postgres", "5432"]).decode().strip().rsplit(":", 1)[1])
        engine = create_database_engine(URL.create("postgresql+psycopg", username="backup_test", password="isolated-backup-test", host="127.0.0.1", port=port, database="backup_test"))
        migration = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        def upgrade(revision):
            with engine.connect() as connection:
                migration.attributes["connection"] = connection
                command.upgrade(migration, revision)
        upgrade("h4f6a8b0c213")
        sql("UPDATE knowledge_bases SET description='before'; INSERT INTO pgvector_collections(id,knowledge_base_id,name,dimension) VALUES('11111111-1111-4111-8111-111111111111','00000000-0000-0000-0000-000000000001','test',3); INSERT INTO pgvector_embeddings(collection_id,id,document,metadata,embedding) VALUES('11111111-1111-4111-8111-111111111111','vector-1','before','{}','[1,2,3]');")
        docker(["exec", "-T", "api", "python", "-c", "from pathlib import Path; p=Path('/app/data/docs'); (p/'history').mkdir(); (p/'images').mkdir(); (p/'.app-backups').mkdir(); (p/'history/old.txt').write_text('before'); (p/'images/a.png').write_bytes(b'PNG-original'); (p/'.app-backups/inner.zip').write_bytes(b'inner-backup')"])
        report = service.backup(tmp_path / "off-host-mounted-backups")
        artifact = Path(report["backup"])
        assert (artifact / "env.original").is_file()
        assert set(report["services_restored"]) == {"api", "worker"}
        assert sql("SELECT embedding FROM pgvector_embeddings WHERE id='vector-1';") == "[1,2,3]"
        upgrade("head")
        assert sql("SELECT to_regclass('public.graph_filter_views') IS NOT NULL;") == "t"
        # Reproduce the real compatibility failure before exercising the fix:
        # h4's clean list cannot drop a parent referenced by i2's absent table.
        with (artifact / "database.dump").open("rb") as source:
            legacy = subprocess.run(base + ["exec", "-T", "postgres", "sh", "-c",
                'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_restore --clean --if-exists --single-transaction --exit-on-error --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],
                cwd=root, stdin=source, capture_output=True, timeout=120)
        assert legacy.returncode != 0
        assert b"graph_filter_views" in legacy.stderr and b"depend" in legacy.stderr
        sql("UPDATE knowledge_bases SET description='after'; UPDATE pgvector_embeddings SET embedding='[3,2,1]';")
        docker(["exec", "-T", "api", "python", "-c", "from pathlib import Path; p=Path('/app/data/docs'); (p/'history/old.txt').write_text('after'); (p/'newer.txt').write_text('new'); (p/'images/a.png').unlink()"])
        backup._private_write(settings, b'POSTGRES_PASSWORD="isolated-backup-test"\nJWT_SECRET_KEY="new-business"\nANSWER_LLM_MODEL="new-model"\nAPP_PORT="9912"\n', replace=True)
        restored = service.restore(artifact)
        assert restored["infra_credentials"] == "preserved" and restored["business_settings"] == "restored"
        assert Path(restored["protection_backup"]).is_dir()
        assert sql("SELECT description FROM knowledge_bases;") == "before"
        assert sql("SELECT version_num FROM alembic_version;") == "h4f6a8b0c213"
        assert sql("SELECT to_regclass('public.graph_filter_views') IS NULL;") == "t"
        assert sql("SELECT to_regprocedure('public.bump_knowledge_graph_revision()') IS NULL;") == "t"
        upgrade("head")  # The real API performs this automatically on startup.
        assert sql("SELECT to_regclass('public.graph_filter_views') IS NOT NULL;") == "t"
        assert sql("SELECT to_regprocedure('public.bump_knowledge_graph_revision()') IS NOT NULL;") == "t"
        assert sql("SELECT embedding FROM pgvector_embeddings WHERE id='vector-1';") == "[1,2,3]"
        objects = docker(["exec", "-T", "api", "python", "-c", "import json; from pathlib import Path; p=Path('/app/data/docs'); print(json.dumps({x.relative_to(p).as_posix():x.read_text() for x in p.rglob('*') if x.is_file()}))"])
        assert json.loads(objects) == {"history/old.txt": "before", "images/a.png": "PNG-original", ".app-backups/inner.zip": "inner-backup"}
        actual = backup.read_env(settings)
        assert actual["JWT_SECRET_KEY"] == "original-business" and actual["ANSWER_LLM_MODEL"] == "original-model"
        assert actual["POSTGRES_PASSWORD"] == "isolated-backup-test" and actual["APP_PORT"] == "9912"
        running = set(docker(["ps", "--services", "--status", "running"]).decode().split())
        assert running == {"postgres", "api", "worker"}
        assert docker(["exec", "-T", "api", "python", "-c", "import os; print(os.environ['JWT_SECRET_KEY'])"]).decode().strip() == "original-business"
        # Latest archives must include the graph revision function and its
        # statement triggers, then keep those triggers functional on replay.
        from sqlalchemy.orm import Session
        from app.models.knowledge_document import KnowledgeDocument
        with Session(engine) as db:
            db.add(KnowledgeDocument(id="backup-doc", knowledge_base_id="00000000-0000-0000-0000-000000000001",
                source_path="backup-doc.txt", original_filename="backup-doc.txt", file_hash="test", file_size=1))
            db.commit()
        revision = sql("SELECT incarnation || ':' || revision FROM knowledge_graph_revisions;")
        from app.services.graph_revision import GRAPH_TABLES
        trigger_count = str(len(GRAPH_TABLES) * 4)
        assert sql("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal AND t.tgname LIKE 'graph_revision_%';") == trigger_count
        current = Path(service.backup(tmp_path / "current-version-backups")["backup"])
        with (current / "database.dump").open("rb") as source:
            backup.validate_database_toc(service._run(["exec", "-T", "postgres", "pg_restore", "--list"], stdin=source, capture=True))
        sql("UPDATE knowledge_bases SET description='after-current-backup';")
        original_run = service._run

        def corrupt_replay(arguments, **kwargs):
            result = original_run(arguments, **kwargs)
            if arguments[:4] == ["exec", "-T", "postgres", "pg_restore"] and "--file=-" in arguments:
                kwargs["stdout"].write(b"\nSELECT 1/0;\n")
            return result

        service._run = corrupt_replay
        try:
            with pytest.raises(backup.BackupError):
                service._database_restore(current / "database.dump")
        finally:
            service._run = original_run
        # DROP TABLE and data replay were in the same transaction: injected SQL
        # failure leaves every pre-restore row/schema intact.
        assert sql("SELECT description FROM knowledge_bases;") == "after-current-backup"
        assert sql("SELECT to_regclass('public.graph_filter_views') IS NOT NULL;") == "t"
        assert sql("SELECT incarnation || ':' || revision FROM knowledge_graph_revisions;") == revision
        service.restore(current)
        assert sql("SELECT description FROM knowledge_bases;") == "before"
        assert sql("SELECT to_regclass('public.graph_filter_views') IS NOT NULL;") == "t"
        assert sql("SELECT incarnation || ':' || revision FROM knowledge_graph_revisions;") == revision
        assert sql("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal AND t.tgname LIKE 'graph_revision_%';") == trigger_count
        sql("UPDATE knowledge_documents SET original_filename='after-restore.txt' WHERE id='backup-doc';")
        assert sql("SELECT revision FROM knowledge_graph_revisions;") == str(int(revision.rsplit(":", 1)[1]) + 1)
        sql("CREATE FUNCTION public.bump_knowledge_graph_revision(text) RETURNS text LANGUAGE sql AS $$ SELECT $1; $$;")
        try:
            with pytest.raises(backup.BackupError, match="dedicated"):
                service._database_scope()
        finally:
            sql("DROP FUNCTION public.bump_knowledge_graph_revision(text);")
        sql("CREATE TABLE unrelated_data(value text); INSERT INTO unrelated_data VALUES('preserve');")
        try:
            with pytest.raises(backup.BackupError, match="outside"):
                service.restore(current)
            assert sql("SELECT value FROM unrelated_data;") == "preserve"
        finally:
            sql("DROP TABLE unrelated_data;")
    finally:
        if engine is not None:
            engine.dispose()
        # This file hard-codes the random drill project and its own two volumes.
        # Never use the real deployment compose file or default project here.
        assert config.parent.parent == root and name.startswith("rag-backup-drill-")
        docker(["down", "--volumes", "--remove-orphans"])

