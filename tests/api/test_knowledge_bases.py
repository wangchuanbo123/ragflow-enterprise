from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME
from app.core.security import hash_password
from app.models.user import User


def _login(client, username, password):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200


def test_admin_can_create_and_list_knowledge_bases(client):
    import app.core.database as db_module
    from app.models.graph_governance import GraphSchemaDefinition

    _login(client, INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)

    created = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Engineering", "slug": "engineering"},
    )

    assert created.status_code == 201
    knowledge_base_id = created.json()["id"]
    listed = client.get("/api/v1/knowledge-bases")
    assert listed.status_code == 200
    assert knowledge_base_id in {item["id"] for item in listed.json()}
    with db_module.SessionLocal() as db:
        active = db.query(GraphSchemaDefinition).filter_by(
            knowledge_base_id=knowledge_base_id,
            status="active",
        ).all()
        assert len(active) == 1


def test_knowledge_base_can_be_edited_and_archived(client):
    _login(client, INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)
    created = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Lifecycle", "slug": "lifecycle-kb"},
    ).json()

    updated = client.patch(
        f"/api/v1/knowledge-bases/{created['id']}",
        json={"name": "Lifecycle Updated", "description": "用于生命周期测试"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Lifecycle Updated"
    assert updated.json()["description"] == "用于生命周期测试"

    archived = client.delete(f"/api/v1/knowledge-bases/{created['id']}")
    assert archived.status_code == 204
    listed_ids = {item["id"] for item in client.get("/api/v1/knowledge-bases").json()}
    assert created["id"] not in listed_ids


def test_knowledge_base_internal_slug_is_generated_automatically(client):
    _login(client, INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)

    generated = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "613"},
    )
    legacy_short_slug = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "中文知识空间", "slug": "6"},
    )

    assert generated.status_code == 201
    assert len(generated.json()["slug"]) >= 3
    assert legacy_short_slug.status_code == 201
    assert legacy_short_slug.json()["slug"].startswith("kb-")


def test_non_member_cannot_discover_another_knowledge_base(client):
    import app.core.database as db_module

    _login(client, INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)
    created = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Private", "slug": "private-kb"},
    )
    knowledge_base_id = created.json()["id"]

    with db_module.SessionLocal() as db:
        user = User(
            username="kb-outsider",
            password_hash=hash_password("outsider-pass"),
            display_name="Outsider",
            role="user",
            is_active=True,
        )
        db.add(user)
        db.commit()

    _login(client, "kb-outsider", "outsider-pass")
    assert client.get(f"/api/v1/knowledge-bases/{knowledge_base_id}").status_code == 404
    listed = client.get("/api/v1/knowledge-bases")
    assert knowledge_base_id not in {item["id"] for item in listed.json()}


def test_conversation_selects_runtime_for_its_knowledge_base(client, monkeypatch):
    from tests.conftest import make_fake_runtime

    runtime = make_fake_runtime(answer="Scoped answer")
    selected = []

    def select_runtime(knowledge_base_id):
        selected.append(knowledge_base_id)
        return runtime

    monkeypatch.setattr(
        "app.services.rag_service.active_runtime",
        select_runtime,
        raising=True,
    )
    _login(client, INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Runtime Scope", "slug": "runtime-scope"},
    ).json()
    conversation = client.post(
        "/api/v1/conversations",
        json={
            "title": "Scoped conversation",
            "knowledge_base_id": knowledge_base["id"],
        },
    )
    assert conversation.status_code == 201
    assert conversation.json()["knowledge_base_id"] == knowledge_base["id"]

    response = client.post(
        f"/api/v1/conversations/{conversation.json()['id']}/messages",
        json={"content": "Question"},
    )
    assert response.status_code == 200
    assert response.json()["assistant_message"]["content"] == "Scoped answer"
    assert selected == [knowledge_base["id"]]


def test_folder_and_tag_endpoints_are_knowledge_base_scoped(client):
    _login(client, INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Managed", "slug": "managed-kb"},
    ).json()
    kb_id = knowledge_base["id"]

    folder = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/folders",
        json={"name": "Policies"},
    )
    tag = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/tags",
        json={"name": "approved", "color": "#00aa00"},
    )

    assert folder.status_code == 201
    assert folder.json()["path"] == "/Policies"
    assert tag.status_code == 201
    assert tag.json()["name"] == "approved"
    assert len(client.get(f"/api/v1/knowledge-bases/{kb_id}/folders").json()) == 1
    assert len(client.get(f"/api/v1/knowledge-bases/{kb_id}/tags").json()) == 1


def test_batch_upload_job_events_and_cancel(client):
    _login(client, INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Batch", "slug": "batch-kb"},
    ).json()
    kb_id = knowledge_base["id"]

    uploaded = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/batch-upload",
        files=[
            ("files", ("one.txt", b"first", "text/plain")),
            ("files", ("two.md", b"second", "text/markdown")),
            ("files", ("bad.exe", b"bad", "application/octet-stream")),
        ],
    )

    assert uploaded.status_code == 202
    payload = uploaded.json()
    assert payload["accepted"] == 2
    assert payload["status"] == "partial"
    assert payload["rejected"][0]["filename"] == "bad.exe"

    job_id = payload["job_id"]
    events = client.get(
        f"/api/v1/knowledge-bases/{kb_id}/index-jobs/{job_id}/events"
    )
    assert events.status_code == 200
    assert events.json()[0]["event_type"] == "created"

    cancelled = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/index-jobs/{job_id}/cancel"
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancel_requested"] is True

    retried = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/index-jobs/{job_id}/retry"
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "pending"
    assert retried.json()["cancel_requested"] is False


def test_member_and_data_source_management(client, monkeypatch):
    import app.core.database as db_module

    _login(client, INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Connectors", "slug": "connectors-kb"},
    ).json()
    kb_id = knowledge_base["id"]
    import json
    monkeypatch.setenv("CONNECTOR_CREDENTIALS_JSON", json.dumps({"docs": {
        "knowledge_base_ids": [kb_id], "source_type": "git",
        "allowed_locations": ["https://example.invalid/docs.git"],
        "secret_ref": "env://TEST_DOCS_TOKEN",
    }}))

    with db_module.SessionLocal() as db:
        db.add(User(
            username="knowledge-viewer",
            password_hash=hash_password("viewer-pass"),
            display_name="Knowledge Viewer",
            role="user",
            is_active=True,
        ))
        db.commit()

    member = client.put(
        f"/api/v1/knowledge-bases/{kb_id}/members",
        json={"username": "knowledge-viewer", "role": "viewer"},
    )
    assert member.status_code == 200
    assert member.json()["role"] == "viewer"
    assert any(
        item["username"] == "knowledge-viewer"
        for item in client.get(f"/api/v1/knowledge-bases/{kb_id}/members").json()
    )

    source = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/sources",
        json={
            "source_type": "git",
            "name": "Engineering docs",
            "connection_ref": "credential://docs",
            "config": {"location": "https://example.invalid/docs.git"},
        },
    )
    assert source.status_code == 201
    assert source.json()["config"]["location"].endswith("docs.git")
    source_id = source.json()["id"]

    checked = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/sources/{source_id}/check"
    )
    assert checked.status_code == 200
    assert checked.json()["health_status"] == "configured"
    assert checked.json()["last_checked_at"] is not None

    unsafe_local = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/sources",
        json={
            "source_type": "local_directory",
            "name": "Outside allowed roots",
            "config": {"location": "../outside"},
        },
    )
    assert unsafe_local.status_code == 400

    rejected = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/sources",
        json={
            "source_type": "s3",
            "name": "Unsafe",
            "config": {"access_key": "must-not-be-stored"},
        },
    )
    assert rejected.status_code == 422

    disabled = client.patch(
        f"/api/v1/knowledge-bases/{kb_id}/sources/{source_id}",
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    assert client.delete(
        f"/api/v1/knowledge-bases/{kb_id}/sources/{source_id}"
    ).status_code == 204
    assert client.delete(
        f"/api/v1/knowledge-bases/{kb_id}/members/{member.json()['user_id']}"
    ).status_code == 204


def test_retrieval_debug_uses_shared_graph_without_generating_answer(client, monkeypatch):
    from tests.conftest import make_fake_runtime

    runtime = make_fake_runtime(answer="rewritten query")
    monkeypatch.setattr(
        "app.services.rag_service.active_runtime",
        lambda: runtime,
        raising=True,
    )
    _login(client, INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)
    knowledge_base_id = next(
        item["id"]
        for item in client.get("/api/v1/knowledge-bases").json()
        if item["is_default"]
    )

    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/retrieval/debug",
        json={"query": "检索调试", "retrieval": {"channels": ["text"]}},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["route"]["selected_channels"] == ["text"]
    assert payload["sources"][0]["source"] == "primary.txt"
    assert "answer" not in payload


def test_local_directory_sync_is_incremental_and_versions_can_be_restored(
    client,
    monkeypatch,
    tmp_path,
):
    _login(client, INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Local sync", "slug": "local-sync"},
    ).json()
    kb_id = knowledge_base["id"]

    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "guide.txt").write_text("version one", encoding="utf-8")
    nested = source_root / "nested"
    nested.mkdir()
    (nested / "policy.md").write_text("# policy", encoding="utf-8")
    (source_root / "ignored.exe").write_bytes(b"ignored")
    monkeypatch.setattr(
        "app.services.data_source_service.LOCAL_SOURCE_ALLOWED_ROOTS",
        (tmp_path.resolve(),),
    )
    import json
    monkeypatch.setenv("LOCAL_SOURCE_ROOTS_JSON", json.dumps({kb_id: [str(source_root.resolve())]}))

    source = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/sources",
        json={
            "source_type": "local_directory",
            "name": "Local documentation",
            "config": {"location": str(source_root)},
        },
    )
    assert source.status_code == 201
    source_id = source.json()["id"]

    first = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/sources/{source_id}/sync"
    )
    assert first.status_code == 202
    assert first.json()["accepted"] == 2
    assert first.json()["skipped"] == 0

    documents = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()
    assert len(documents) == 2
    assert {item["metadata_json"]["source_relative_path"] for item in documents} == {
        "guide.txt",
        "nested/policy.md",
    }

    unchanged = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/sources/{source_id}/sync"
    )
    assert unchanged.status_code == 202
    assert unchanged.json()["status"] == "unchanged"
    assert unchanged.json()["accepted"] == 0
    assert unchanged.json()["skipped"] == 2

    (source_root / "guide.txt").write_text("version two", encoding="utf-8")
    changed = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/sources/{source_id}/sync"
    )
    assert changed.status_code == 202
    assert changed.json()["accepted"] == 1
    guide = next(item for item in documents if item["original_filename"] == "guide.txt")
    versions_url = f"/api/v1/knowledge-bases/{kb_id}/documents/{guide['id']}/versions"
    versions = client.get(versions_url).json()
    assert [item["version_no"] for item in versions] == [2, 1]

    restored = client.post(
        f"{versions_url}/{versions[-1]['id']}/restore"
    )
    assert restored.status_code == 202
    assert restored.json()["version_no"] == 3
    assert restored.json()["restored_from_version_id"] == versions[-1]["id"]
