"""Startup migration safety, with synthetic files and no Docker/service access."""

import json
from pathlib import Path, PurePosixPath
import subprocess

import pytest

from scripts import start_server


ORIGINAL_DEPLOYMENT = "1c3770e0-20eb-4f9b-a94c-956f9eab0c42"
NEW_DEPLOYMENT = "7bcb6294-743b-4e5a-86ed-c53c9b68cbd7"


@pytest.fixture(autouse=True)
def isolated_startup_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(start_server, "ROOT", tmp_path)


def legacy_tree(root, *, data=None, database=None, objects=None, environment=""):
    root.mkdir(parents=True, exist_ok=True)
    data = data or root / "data"
    database = database or data / "app.db"
    objects = objects or data / "docs"
    vectors = data / "vector_db"
    database.parent.mkdir(parents=True, exist_ok=True)
    objects.mkdir(parents=True, exist_ok=True)
    vectors.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"synthetic SQLite source - subprocess is mocked")
    document = objects / "original.txt"
    document.write_bytes(b"public synthetic document")
    vector = vectors / "chroma.sqlite3"
    vector.write_bytes(b"synthetic original vector state")
    (root / ".env").write_text(environment, encoding="utf-8")
    return {"database": database.resolve(), "objects": objects.resolve(), "vectors": vectors.resolve(),
            "originals": {path: path.read_bytes() for path in (database, document, vector, root / ".env")}}


def fake_docker(monkeypatch, *, deployment="missing", reject_verification=False):
    commands = []
    state = {"deployment": deployment}
    def run(command, **kwargs):
        command = [str(value) for value in command]
        commands.append(command)
        output, code = "", 0
        if "scripts.server_identity" in command:
            if "--ensure" in command and state["deployment"] == "missing":
                state["deployment"] = NEW_DEPLOYMENT
            output = state["deployment"] + "\n"
        if reject_verification and "scripts.migrate_to_postgres" in command and "--verify-only" in command:
            code = 23
        text_mode = kwargs.get("text") or kwargs.get("encoding")
        return subprocess.CompletedProcess(command, code, stdout=output if text_mode else output.encode(),
                                           stderr="synthetic migration mismatch" if text_mode else b"synthetic migration mismatch")
    monkeypatch.setattr(start_server.subprocess, "run", run)
    return commands


def migration_commands(commands):
    return [command for command in commands if "scripts.migrate_to_postgres" in command]


def resolve_mounted_source(command, flag):
    requested = PurePosixPath(command[command.index(flag) + 1])
    candidates = []
    for index, item in enumerate(command[:-1]):
        if item not in {"-v", "--volume"}:
            continue
        specification = command[index + 1]
        if not specification.endswith(":ro"):
            continue
        host, target, mode = specification.rsplit(":", 2)
        mounted = PurePosixPath(target)
        if requested.is_relative_to(mounted):
            candidates.append((len(mounted.parts), (Path(host) / Path(*requested.relative_to(mounted).parts)).resolve(), mode))
    assert candidates, f"Legacy source {flag} must be mounted read-only"
    return max(candidates, key=lambda item: item[0])[1]


def assert_originals_unchanged(layout):
    for path, content in layout["originals"].items():
        assert path.read_bytes() == content


def test_matching_completion_marker_is_checked_against_target_identity(tmp_path, monkeypatch):
    layout = legacy_tree(tmp_path / "project")
    reports = tmp_path / "project/data/server-migration"
    reports.mkdir(parents=True)
    (reports / "complete.json").write_text(json.dumps({"status": "complete", "deployment_id": ORIGINAL_DEPLOYMENT}), encoding="utf-8")
    commands = fake_docker(monkeypatch, deployment=ORIGINAL_DEPLOYMENT)
    start_server.import_legacy(["docker", "compose"], tmp_path / "project")
    assert any("scripts.server_identity" in command and "--read" in command for command in commands)
    assert not [command for command in migration_commands(commands) if "--verify-only" not in command]
    assert_originals_unchanged(layout)


@pytest.mark.parametrize("target", [NEW_DEPLOYMENT, "missing"])
def test_recreated_or_missing_target_identity_cannot_trust_old_local_marker(tmp_path, monkeypatch, target):
    root = tmp_path / "project"
    layout = legacy_tree(root)
    reports = root / "data/server-migration"
    reports.mkdir(parents=True)
    marker = reports / "complete.json"
    marker.write_text(json.dumps({"status": "complete", "deployment_id": ORIGINAL_DEPLOYMENT}), encoding="utf-8")
    # An old metadata report is also not proof that the new target has data.
    (reports / "metadata.json").write_text('{"source":"old-deployment"}', encoding="utf-8")
    commands = fake_docker(monkeypatch, deployment=target, reject_verification=True)
    rejected = False
    try:
        start_server.import_legacy(["docker", "compose"], root)
    except RuntimeError as exc:
        rejected = True
        assert str(exc)
    assert any("scripts.server_identity" in command and "--read" in command for command in commands)
    assert rejected or any("--verify-only" not in command for command in migration_commands(commands))
    assert_originals_unchanged(layout)


@pytest.mark.parametrize("marker_content", [
    '{"status":"complete","source_preserved":true}',
    '{"status":"complete","deployment_id":null}',
    '{invalid-json',
])
def test_legacy_or_damaged_marker_cannot_silently_skip_verification(tmp_path, monkeypatch, marker_content):
    root = tmp_path / "project"
    legacy_tree(root)
    reports = root / "data/server-migration"
    reports.mkdir(parents=True)
    (reports / "complete.json").write_text(marker_content, encoding="utf-8")
    commands = fake_docker(monkeypatch, deployment=ORIGINAL_DEPLOYMENT)
    rejected = False
    try:
        start_server.import_legacy(["docker", "compose"], root)
    except (RuntimeError, ValueError):
        rejected = True
    assert rejected or migration_commands(commands)


@pytest.mark.parametrize("variant", ["custom_data", "relative_sqlite", "absolute_sqlite", "custom_objects"])
def test_import_resolves_legacy_paths_and_mounts_every_source_read_only(tmp_path, monkeypatch, variant):
    root = tmp_path / "project"
    data = root / "custom data"
    database = data / "app.db"
    objects = data / "docs"
    settings = {"DATA_DIR": "custom data"}
    if variant == "relative_sqlite":
        database = root / "database elsewhere" / "legacy.sqlite"
        settings["DATABASE_URL"] = "sqlite:///database elsewhere/legacy.sqlite"
    elif variant == "absolute_sqlite":
        database = tmp_path / "external database" / "legacy.sqlite"
        settings["DATABASE_URL"] = "sqlite:///" + database.as_posix()
    elif variant == "custom_objects":
        objects = root / "objects elsewhere"
        settings["LOCAL_OBJECT_STORE_DIR"] = "objects elsewhere"
    environment = "\n".join(key + "=" + json.dumps(value) for key, value in settings.items()) + "\n"
    layout = legacy_tree(root, data=data, database=database, objects=objects, environment=environment)
    commands = fake_docker(monkeypatch)
    start_server.import_legacy(["docker", "compose"], root)
    metadata = migration_commands(commands)
    assert metadata, "Custom DATA_DIR / DATABASE_URL must not silently appear to be an empty project"
    assert resolve_mounted_source(metadata[0], "--source-sqlite") == layout["database"]
    object_import = next(command for command in commands if "scripts.import_legacy_objects" in command)
    vector_import = next(command for command in commands if "scripts.migrate_vectors_to_pgvector" in command)
    assert resolve_mounted_source(object_import, "--source") == layout["objects"]
    assert resolve_mounted_source(vector_import, "--source") == layout["vectors"]
    assert "--active-only" in vector_import
    assert_originals_unchanged(layout)


@pytest.mark.parametrize("url", [
    "postgresql://legacy-user:synthetic-private-password@legacy-host:5432/legacy",
    "mysql://legacy-user:synthetic-private-password@legacy-host/legacy",
    "sqlite:///:memory:",
], ids=["postgresql", "mysql", "memory"])
def test_non_file_sqlite_legacy_sources_are_rejected_without_credentials(tmp_path, monkeypatch, url):
    root = tmp_path / "project"
    legacy_tree(root, environment="DATABASE_URL=" + json.dumps(url) + "\n")
    fake_docker(monkeypatch)
    with pytest.raises((RuntimeError, ValueError)) as caught:
        start_server.import_legacy(["docker", "compose"], root)
    assert "synthetic-private-password" not in str(caught.value)


def test_quiet_failure_identifies_safe_step_and_exit_code_without_raw_output(monkeypatch, capsys, tmp_path):
    command = ["docker", "compose", "run", "api", "python", "-m", "scripts.migrate_to_postgres"]
    real_run = subprocess.run
    def fail(actual, **kwargs):
        if actual[0] != "docker":
            return real_run(actual, **kwargs)
        output = "authorization=synthetic-private-secret"
        error = "MigrationError: synthetic-private-secret"
        return subprocess.CompletedProcess(command, 23, stdout=output if kwargs.get("text") else output.encode(),
                                           stderr=error if kwargs.get("text") else error.encode())
    monkeypatch.setattr(start_server.subprocess, "run", fail)
    with pytest.raises(RuntimeError) as caught:
        start_server.checked(command, quiet=True)
    assert "migrate_to_postgres" in str(caught.value) and "23" in str(caught.value)
    captured = capsys.readouterr()
    assert "synthetic-private-secret" not in str(caught.value) + captured.out + captured.err
    assert "MigrationError" in (tmp_path / "data/server-start-failure.log").read_text(encoding="utf-8")


def mocked_quiet_failure(monkeypatch):
    command = ["synthetic-docker", "compose", "run", "api", "python", "-m", "scripts.migrate_to_postgres"]
    real_run = subprocess.run
    def run(actual, **kwargs):
        if actual[0] != "synthetic-docker":
            return real_run(actual, **kwargs)
        output = "synthetic-private-secret"
        return subprocess.CompletedProcess(actual, 23, stdout=output if kwargs.get("text") else output.encode(),
                                           stderr=output if kwargs.get("text") else output.encode())
    monkeypatch.setattr(start_server.subprocess, "run", run)
    return command


def test_quiet_diagnostics_restrict_existing_log_before_writing_new_secrets(tmp_path, monkeypatch):
    from scripts import server_backup
    target = tmp_path / "data/server-start-failure.log"
    target.parent.mkdir()
    target.write_text("public old diagnostic", encoding="utf-8")
    target.chmod(0o666)
    checked_before_write = []
    restrict = server_backup._restrict_private
    def enforce(path, **kwargs):
        checked_before_write.append(path.read_bytes())
        return restrict(path, **kwargs)
    monkeypatch.setattr(server_backup, "_restrict_private", enforce)
    command = mocked_quiet_failure(monkeypatch)
    with pytest.raises(RuntimeError):
        start_server.checked(command, quiet=True)
    assert checked_before_write == [b""]
    assert "synthetic-private-secret" in target.read_text(encoding="utf-8")
    if start_server.os.name != "nt":
        assert target.stat().st_mode & 0o077 == 0


def test_quiet_diagnostics_refuse_link_target_and_keep_failure_explanation(tmp_path, monkeypatch, capsys):
    target = tmp_path / "data/server-start-failure.log"
    target.parent.mkdir()
    target.write_bytes(b"must not be overwritten")
    original = Path.is_symlink
    # Simulate this boundary on Windows without requiring symlink privileges.
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or original(path))
    command = mocked_quiet_failure(monkeypatch)
    with pytest.raises(RuntimeError) as caught:
        start_server.checked(command, quiet=True)
    assert target.read_bytes() == b"must not be overwritten"
    assert "migrate_to_postgres" in str(caught.value) and "23" in str(caught.value)
    assert "无法建立私有诊断日志" in str(caught.value)
    captured = capsys.readouterr()
    assert "synthetic-private-secret" not in str(caught.value) + captured.out + captured.err


def test_quiet_diagnostics_permission_failure_never_falls_back_to_public_output(tmp_path, monkeypatch, capsys):
    from scripts import server_backup
    def deny(*args, **kwargs):
        raise PermissionError("synthetic-private-secret")
    monkeypatch.setattr(server_backup, "_restrict_private", deny)
    command = mocked_quiet_failure(monkeypatch)
    with pytest.raises(RuntimeError) as caught:
        start_server.checked(command, quiet=True)
    assert "migrate_to_postgres" in str(caught.value) and "23" in str(caught.value)
    assert "无法建立私有诊断日志" in str(caught.value)
    assert list((tmp_path / "data").iterdir()) == []
    captured = capsys.readouterr()
    assert "synthetic-private-secret" not in str(caught.value) + captured.out + captured.err


def test_matching_marker_allows_original_sqlite_to_be_archived(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text('DATABASE_URL="sqlite:///archived/old.sqlite"\n', encoding="utf-8")
    reports = root / "data/server-migration"
    reports.mkdir(parents=True)
    (reports / "complete.json").write_text(json.dumps({"status": "complete", "deployment_id": ORIGINAL_DEPLOYMENT}), encoding="utf-8")
    commands = fake_docker(monkeypatch, deployment=ORIGINAL_DEPLOYMENT)
    start_server.import_legacy(["docker", "compose"], root)
    assert any("scripts.server_identity" in command and "--read" in command for command in commands)
    assert not migration_commands(commands)


def test_missing_explicit_legacy_data_directory_does_not_silently_start_empty(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text('DATA_DIR="missing custom data"\n', encoding="utf-8")
    commands = fake_docker(monkeypatch)
    with pytest.raises(RuntimeError):
        start_server.import_legacy(["docker", "compose"], root)
    assert not migration_commands(commands)


@pytest.mark.parametrize("pending_target, expected_verify", [(ORIGINAL_DEPLOYMENT, True), (NEW_DEPLOYMENT, False)])
def test_resume_verifies_only_when_pending_report_belongs_to_current_target(tmp_path, monkeypatch, pending_target, expected_verify):
    root = tmp_path / "project"
    legacy_tree(root)
    reports = root / "data/server-migration"
    reports.mkdir(parents=True)
    (reports / "pending.json").write_text(json.dumps({"deployment_id": pending_target, "phase": "metadata"}), encoding="utf-8")
    (reports / "metadata.json").write_text('{"status":"copied"}', encoding="utf-8")
    commands = fake_docker(monkeypatch, deployment=ORIGINAL_DEPLOYMENT)
    try:
        start_server.import_legacy(["docker", "compose"], root)
    except RuntimeError:
        assert not expected_verify
        return
    metadata = migration_commands(commands)
    assert metadata
    assert ("--verify-only" in metadata[0]) is expected_verify


def test_new_project_without_default_sqlite_may_start_without_import(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("DATABASE_URL=sqlite:///data/app.db\n", encoding="utf-8")
    commands = fake_docker(monkeypatch)
    start_server.import_legacy(["docker", "compose"], root)
    assert not migration_commands(commands)


def test_application_services_are_not_started_after_migration_failure(monkeypatch, capsys):
    commands = []
    monkeypatch.setattr(start_server.shutil, "which", lambda _: "synthetic-docker")
    monkeypatch.setattr(start_server, "configure_server", lambda: None)
    monkeypatch.setattr(start_server, "compose_command", lambda: ["synthetic-docker", "compose"])
    monkeypatch.setattr(start_server, "checked", lambda command, **kwargs: commands.append(command))
    def reject_import(*args):
        raise RuntimeError("scripts.migrate_to_postgres 启动步骤失败，exit code 23；已有数据保留")
    monkeypatch.setattr(start_server, "import_legacy", reject_import)
    monkeypatch.setattr(start_server.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("Failed import must not poll an application"))
    assert start_server.main(["--skip-build", "--no-browser"]) == 1
    assert commands == [
        ["synthetic-docker", "compose", "config", "--quiet"],
        ["synthetic-docker", "compose", "up", "-d", "--wait", "postgres", "redis"],
    ]
    assert "migrate_to_postgres" in capsys.readouterr().out


def test_legacy_build_detection_only_requires_packages_for_existing_vector_import(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    assert not start_server.legacy_image_required(root)
    layout = legacy_tree(root)
    assert start_server.legacy_image_required(root)
    (layout["vectors"] / "chroma.sqlite3").unlink()
    assert not start_server.legacy_image_required(root)


def test_archived_legacy_source_does_not_block_image_capability_detection(tmp_path):
    (tmp_path / ".env").write_text('DATABASE_URL="sqlite:///archived/old.sqlite"\n', encoding="utf-8")
    assert not start_server.legacy_image_required(tmp_path)


@pytest.mark.parametrize("has_legacy", [True, False])
def test_startup_builds_migration_capability_then_removes_legacy_from_runtime(monkeypatch, has_legacy):
    commands, phases = [], []
    monkeypatch.setattr(start_server.shutil, "which", lambda _: "synthetic-docker")
    monkeypatch.setattr(start_server, "configure_server", lambda: None)
    monkeypatch.setattr(start_server, "compose_command", lambda: ["synthetic-docker", "compose"])
    monkeypatch.setattr(start_server, "legacy_image_required", lambda root: has_legacy)
    def checked(command, **kwargs):
        commands.append(command)
        phases.append("build" if "build" in command else "other")
    monkeypatch.setattr(start_server, "checked", checked)
    monkeypatch.setattr(start_server, "import_legacy", lambda base: phases.append("import"))
    class Ready:
        def __enter__(self):
            import io
            return io.BytesIO(b'{"status":"ready"}')
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(start_server.urllib.request, "urlopen", lambda *args, **kwargs: Ready())
    assert start_server.main(["--no-browser"]) == 0
    builds = [command for command in commands if "build" in command]
    if has_legacy:
        assert builds == [["synthetic-docker", "compose", "build", "--build-arg", "INSTALL_LEGACY=true", "api"],
                          ["synthetic-docker", "compose", "build", "--build-arg", "INSTALL_LEGACY=false", "api"]]
        assert phases.index("build") < phases.index("import") < len(phases) - 1
        assert phases[phases.index("import") + 1] == "build"
    else:
        assert builds == [["synthetic-docker", "compose", "build", "api"]]
