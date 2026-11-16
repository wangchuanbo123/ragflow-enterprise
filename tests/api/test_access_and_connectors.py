"""Regression coverage for knowledge-base access and connector trust boundaries."""

import json
import io
import sys
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.core.security import hash_password
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseMember
from app.models.knowledge_document import KnowledgeDocument
from app.models.user import User
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.services.credential_service import CredentialResolver
from app.services.data_source_service import DataSourceService


def _login(client, username=INITIAL_ADMIN_USERNAME, password=INITIAL_ADMIN_PASSWORD):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200


def _member(client, role="editor"):
    from app.core.database import SessionLocal

    _login(client)
    kb = client.post("/api/v1/knowledge-bases", json={"name": "Restricted", "slug": "restricted"}).json()
    with SessionLocal() as db:
        user = User(username="member", display_name="Member", role="user", password_hash=hash_password("member-pass"))
        db.add(user)
        db.flush()
        db.add(KnowledgeBaseMember(knowledge_base_id=kb["id"], user_id=user.id, role=role))
        db.commit()
        user_id = user.id
    _login(client, "member", "member-pass")
    return kb["id"], user_id


def test_legacy_endpoints_require_default_kb_membership(client):
    _member(client, "viewer")
    paths = ["/api/v1/documents", "/api/v1/documents/unknown", "/api/v1/index-jobs",
             "/api/v1/index-jobs/unknown", "/api/v1/knowledge-graph/stats",
             "/api/v1/knowledge-graph/entities?query=secret", "/api/v1/knowledge-graph/entities/unknown",
             "/api/v1/knowledge-graph/entities/unknown/neighbors"]
    for path in paths:
        assert client.get(path).status_code == 404, path
    assert client.post("/ask", json={"query": "secret"}).status_code == 404


def test_archived_knowledge_base_revokes_members_and_scheduled_sources(client):
    from app.core.database import SessionLocal

    kb_id, user_id = _member(client)
    with SessionLocal() as db:
        repo = KnowledgeBaseRepository(db)
        source = repo.create_data_source(kb_id, source_type="git", name="docs", connection_ref=None,
                                        config={"location": "https://example.invalid/docs"}, sync_interval_minutes=1)
        assert source in repo.list_due_data_sources(__import__("datetime").datetime.utcnow())
        repo.archive(repo.get(kb_id))
        assert not repo.can_read(kb_id, user_id)
        assert not repo.can_edit(kb_id, user_id)
        assert repo.list_due_data_sources(__import__("datetime").datetime.utcnow()) == []
    assert client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").status_code == 404
    assert client.post(f"/api/v1/knowledge-bases/{kb_id}/folders", json={"name": "forbidden"}).status_code == 403


@pytest.mark.parametrize("role,editable", [("viewer", False), ("editor", True), ("owner", True)])
def test_knowledge_base_exposes_current_user_capabilities(client, role, editable):
    kb_id, _ = _member(client, role)
    kb = client.get(f"/api/v1/knowledge-bases/{kb_id}").json()
    assert kb["current_user_role"] == role
    assert kb["capabilities"]["can_edit"] is editable
    assert kb["capabilities"]["can_manage_members"] is (role == "owner")


@pytest.mark.parametrize("reference", ["env://JWT_SECRET_KEY", "file://C:/secret.txt", "vault://secret"])
def test_editors_cannot_reference_arbitrary_secrets(client, reference):
    kb_id, _ = _member(client)
    response = client.post(f"/api/v1/knowledge-bases/{kb_id}/sources", json={
        "source_type": "git", "name": "untrusted", "connection_ref": reference,
        "config": {"location": "https://attacker.invalid/repository"},
    })
    assert response.status_code == 400


def _registry(monkeypatch, kb_id="kb"):
    registry = {"docs": {"secret_ref": "env://TEST_CONNECTOR_TOKEN", "source_type": "git",
                          "knowledge_base_ids": [kb_id], "allowed_locations": ["https://git.example/team/docs.git"]}}
    monkeypatch.setenv("CONNECTOR_CREDENTIALS_JSON", json.dumps(registry))
    monkeypatch.setenv("TEST_CONNECTOR_TOKEN", "synthetic-test-token")
    return registry


def test_registered_credentials_are_bound_to_kb_destination_and_protocol(monkeypatch):
    _registry(monkeypatch)
    config = {"location": "https://git.example/team/docs.git"}
    assert CredentialResolver.resolve("credential://docs", knowledge_base_id="kb", source_type="git", config=config) == {"token": "synthetic-test-token"}
    for kb_id, location in [("other-kb", config["location"]), ("kb", "https://attacker.invalid/repo"),
                            ("kb", "https://git.example/team/other.git"), ("kb", config["location"] + "?redirect=1")]:
        with pytest.raises(ValueError):
            CredentialResolver.resolve("credential://docs", knowledge_base_id=kb_id, source_type="git", config={"location": location})


def test_s3_credentials_bind_endpoint_and_never_fall_back_to_ambient_identity(monkeypatch):
    monkeypatch.setenv("CONNECTOR_CREDENTIALS_JSON", json.dumps({"s3": {
        "secret_ref": "env://TEST_S3_KEYS", "knowledge_base_ids": ["kb"], "source_type": "s3",
        "allowed_locations": ["s3://docs/team"], "endpoint_url": "https://objects.example", "region": "test-1",
    }}))
    config = {"location": "s3://docs/team", "endpoint_url": "https://objects.example", "region": "test-1"}
    assert CredentialResolver.validate("credential://s3", knowledge_base_id="kb", source_type="s3", config=config)
    for modified in [{**config, "endpoint_url": "https://attacker.invalid"}, {**config, "region": "other"}]:
        with pytest.raises(ValueError):
            CredentialResolver.validate("credential://s3", knowledge_base_id="kb", source_type="s3", config=modified)
    with pytest.raises(ValueError):
        CredentialResolver.resolve(None, knowledge_base_id="kb", source_type="s3", config=config)


def test_git_does_not_forward_credentials_on_redirects_or_use_global_credential_helpers(monkeypatch, tmp_path):
    _registry(monkeypatch)
    monkeypatch.setenv("SSH_AUTH_SOCK", "ambient-ssh-agent")
    monkeypatch.setenv("GIT_ASKPASS", "ambient-credential-helper")
    (tmp_path / "repo").mkdir()
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="revision")
    monkeypatch.setattr("app.services.data_source_service.subprocess.run", run)
    source = SimpleNamespace(config_json={"location": "https://git.example/team/docs.git"}, connection_ref="credential://docs")
    DataSourceService(None, "kb", task_queue=object())._checkout_git(source, tmp_path)
    command = calls[0][0][0]
    assert "http.followRedirects=false" in command
    assert "http.emptyAuth=false" in command
    assert "credential.helper=" in command
    assert calls[0][1]["env"]["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer synthetic-test-token"
    assert calls[0][1]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert calls[0][1]["env"]["GIT_CONFIG_KEY_0"] == "http.https://git.example/team/docs.git.extraHeader"
    assert "SSH_AUTH_SOCK" not in calls[0][1]["env"]
    assert calls[0][1]["env"]["GIT_ASKPASS"] != "ambient-credential-helper"
    assert calls[0][1]["env"]["HOME"] == str(tmp_path / ".connector-home")
    assert (tmp_path / ".connector-home" / ".netrc").read_text() == ""


@pytest.mark.parametrize("location", ["ssh://git@git.example/team/repo.git", "git@git.example:team/repo.git",
                                      "file:///private/repo", "git://git.example/team/repo.git"])
def test_git_rejects_protocols_that_could_use_ambient_identity(monkeypatch, tmp_path, location):
    calls = []
    monkeypatch.setattr("app.services.data_source_service.subprocess.run", lambda *args, **kwargs: calls.append(args))
    source = SimpleNamespace(config_json={"location": location}, connection_ref=None)
    with pytest.raises(ValueError, match="HTTP"):
        DataSourceService(None, "kb", task_queue=object())._checkout_git(source, tmp_path)
    assert calls == []


def test_git_credentials_cannot_be_sent_over_http_even_when_target_is_registered(monkeypatch):
    registry = _registry(monkeypatch)
    registry["docs"]["allowed_locations"] = ["http://git.example/team/docs.git"]
    monkeypatch.setenv("CONNECTOR_CREDENTIALS_JSON", json.dumps(registry))
    with pytest.raises(ValueError, match="HTTPS"):
        CredentialResolver.resolve("credential://docs", knowledge_base_id="kb", source_type="git",
                                   config={"location": "http://git.example/team/docs.git"})


@pytest.mark.skipif(shutil.which("git") is None, reason="Git executable is required for URL-scoped configuration")
def test_git_auth_header_matches_only_registered_repository(monkeypatch, tmp_path):
    _registry(monkeypatch)
    (tmp_path / "repo").mkdir()
    run_process = subprocess.run
    environments = []
    def capture(*args, **kwargs):
        if "env" in kwargs:
            environments.append(kwargs["env"])
        return SimpleNamespace(stdout="revision")
    monkeypatch.setattr("app.services.data_source_service.subprocess.run", capture)
    source = SimpleNamespace(config_json={"location": "https://git.example/team/docs.git"}, connection_ref="credential://docs")
    DataSourceService(None, "kb", task_queue=object())._checkout_git(source, tmp_path)
    for location, expected in [
        ("https://git.example/team/docs.git/info/refs", True),
        ("https://git.example/team/other.git/info/refs", False),
        ("https://attacker.invalid/team/docs.git/info/refs", False),
    ]:
        result = run_process(["git", "config", "--get-urlmatch", "http.extraHeader", location],
                             env=environments[0], capture_output=True, text=True, check=False)
        assert (result.returncode == 0) is expected
        assert ("synthetic-test-token" in result.stdout) is expected


@pytest.mark.skipif(shutil.which("git") is None, reason="Git executable is required for the connector integration check")
def test_public_git_cannot_use_netrc_or_injected_ambient_auth(monkeypatch, tmp_path):
    ambient = tmp_path / "ambient-home"
    ambient.mkdir()
    for name in (".netrc", "_netrc"):
        (ambient / name).write_text("machine 127.0.0.1 login ambient-user password synthetic-ambient-password\n")
    for name in ("HOME", "USERPROFILE", "CURL_HOME"):
        monkeypatch.setenv(name, str(ambient))
    monkeypatch.setenv("NETRC", str(ambient / ".netrc"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: Basic synthetic-ambient-header")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    seen_authorization = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen_authorization.append(self.headers.get("Authorization"))
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="connector-test"')
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        location = f"http://127.0.0.1:{server.server_port}/repo.git"
        source = SimpleNamespace(config_json={"location": location}, connection_ref=None)
        with pytest.raises(subprocess.CalledProcessError):
            DataSourceService(None, "kb", task_queue=object())._checkout_git(source, tmp_path / "checkout")
        assert seen_authorization and all(header is None for header in seen_authorization)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_credential_catalog_filters_kbs_and_hides_secret_locations(client, monkeypatch):
    kb_id, _ = _member(client)
    registry = _registry(monkeypatch, kb_id)
    registry["other"] = {**registry["docs"], "knowledge_base_ids": ["other-kb"]}
    monkeypatch.setenv("CONNECTOR_CREDENTIALS_JSON", json.dumps(registry))
    response = client.get(f"/api/v1/knowledge-bases/{kb_id}/connector-credentials")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["docs"]
    assert "secret_ref" not in response.text and "TEST_CONNECTOR_TOKEN" not in response.text


def test_directory_sources_require_kb_grants_and_exclude_internal_storage(monkeypatch, tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    internal = tmp_path / "objects"
    private = internal / "knowledge-bases" / "private"
    private.mkdir(parents=True)
    upload = internal / "code_uploads" / "kb"
    upload.mkdir(parents=True)
    monkeypatch.setattr("app.services.data_source_service.DOC_DIR", internal)
    monkeypatch.setattr("app.services.data_source_service.LOCAL_OBJECT_STORE_DIR", internal)
    monkeypatch.setattr("app.services.data_source_service.LOCAL_SOURCE_ALLOWED_ROOTS", [tmp_path])
    monkeypatch.setenv("LOCAL_SOURCE_ROOTS_JSON", json.dumps({"kb": [str(root), str(internal)]}))
    service = DataSourceService(None, "kb", task_queue=object())
    assert service._check("local_directory", {"location": str(root)}, None)[0] == "ok"
    assert service._check("local_directory", {"location": str(private)}, None)[0] == "fail"
    assert service._check("local_directory", {"location": str(upload)}, None)[0] == "ok"
    assert service._check("local_directory", {"location": str(internal)}, None)[0] == "fail"
    assert DataSourceService(None, "other", task_queue=object())._check("local_directory", {"location": str(root)}, None)[0] == "fail"


def test_partial_snapshots_preserve_present_documents_and_complete_snapshots_remove_missing(db_engine):
    from app.core.database import SessionLocal
    from app.adapters.local import InlineTaskQueue
    from app.repositories.index_job_repository import IndexJobRepository
    from app.services.document_index_service import DocumentIndexService

    with SessionLocal() as db:
        db.add(KnowledgeBase.default())
        db.add(KnowledgeDocument(id="present", knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                                source_path="present/doc.txt", original_filename="doc.txt", file_hash="h", file_size=1,
                                status="ready", metadata_json={"data_source_id": "source", "source_relative_path": "doc.txt"}))
        db.commit()
        service = DocumentIndexService(db, object_store=object(), task_queue=InlineTaskQueue())
        partial, *_ = service.create_data_source_sync_job("source", [], rejected=[{"path": "doc.txt", "error": "too large"}], deletion_policy="delete")
        assert IndexJobRepository(db).list_items(partial.id) == []
        assert db.get(KnowledgeDocument, "present").status == "ready"
        page, *_ = service.create_data_source_sync_job("source", [], observed_paths=["doc.txt"], deletion_policy="delete")
        assert IndexJobRepository(db).list_items(page.id) == []
        complete, *_ = service.create_data_source_sync_job("source", [], deletion_policy="delete")
        assert [item.action for item in IndexJobRepository(db).list_items(complete.id)] == ["delete"]


def test_large_directory_scans_advance_and_keep_full_presence_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.data_source_service.MAX_BATCH_UPLOAD_FILES", 2)
    for name in ["a.txt", "b.txt", "c.txt"]:
        (tmp_path / name).write_text(name)
    first, rejected, cursor = DataSourceService._collect_directory(tmp_path, True)
    assert len(first) == 2 and not rejected and cursor["has_more"]
    second, rejected, last = DataSourceService._collect_directory(tmp_path, True, offset=cursor["next_offset"])
    assert [name for name, _ in second] == ["c.txt"]
    assert last["next_offset"] == 0 and last["observed_paths"] == ["a.txt", "b.txt", "c.txt"]


def test_s3_scans_all_metadata_pages_and_advances_download_batches(monkeypatch):
    monkeypatch.setattr("app.services.data_source_service.MAX_BATCH_UPLOAD_FILES", 2)
    monkeypatch.setenv("TEST_S3_KEYS", json.dumps({"access_key": "fake", "secret_key": "fake"}))
    monkeypatch.setenv("CONNECTOR_CREDENTIALS_JSON", json.dumps({"s3": {
        "secret_ref": "env://TEST_S3_KEYS", "knowledge_base_ids": ["kb"], "source_type": "s3",
        "allowed_locations": ["s3://docs/team"],
    }}))
    downloads = []
    class Client:
        def list_objects_v2(self, **kwargs):
            if kwargs.get("ContinuationToken"):
                return {"Contents": [{"Key": "team/c.txt", "Size": 1}], "IsTruncated": False}
            return {"Contents": [{"Key": "team/a.txt", "Size": 1}, {"Key": "team/b.txt", "Size": 1}],
                    "IsTruncated": True, "NextContinuationToken": "page-2"}
        def get_object(self, **kwargs):
            downloads.append(kwargs["Key"])
            return {"Body": io.BytesIO(b"x")}
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda *args, **kwargs: Client()))
    monkeypatch.setitem(sys.modules, "botocore.config", SimpleNamespace(Config=lambda **kwargs: None))
    source = SimpleNamespace(config_json={"location": "s3://docs/team"}, connection_ref="credential://s3", sync_cursor_json={})
    service = DataSourceService(None, "kb", task_queue=object())
    first, rejected, cursor = service._collect_s3(source)
    assert [name for name, _ in first] == ["a.txt", "b.txt"] and not rejected
    assert cursor["observed_paths"] == ["a.txt", "b.txt", "c.txt"]
    source.sync_cursor_json = cursor
    second, rejected, last = service._collect_s3(source)
    assert [name for name, _ in second] == ["c.txt"] and not rejected
    assert last["next_offset"] == 0
    assert downloads == ["team/a.txt", "team/b.txt", "team/c.txt"]
