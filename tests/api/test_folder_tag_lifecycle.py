"""Folder moves and tag deletion keep document associations valid."""

from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.knowledge_document import KnowledgeDocument


def _setup(client):
    assert client.post("/api/v1/auth/login", json={"username": INITIAL_ADMIN_USERNAME,
                                                 "password": INITIAL_ADMIN_PASSWORD}).status_code == 200
    return f"/api/v1/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}"


def test_folder_rename_move_cycles_and_delete_preserve_documents(client):
    from app.core.database import SessionLocal

    base = _setup(client)
    root = client.post(base + "/folders", json={"name": "root"}).json()
    child = client.post(base + "/folders", json={"name": "child", "parent_id": root["id"]}).json()
    with SessionLocal() as db:
        db.add(KnowledgeDocument(id="folder-document", knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                                folder_id=child["id"], source_path="folder-document/test.txt",
                                original_filename="test.txt", file_hash="h", file_size=1))
        db.commit()
    assert client.patch(base + f"/folders/{root['id']}", json={"parent_id": child["id"]}).status_code == 400
    renamed = client.patch(base + f"/folders/{root['id']}", json={"name": "renamed"})
    assert renamed.status_code == 200
    folders = {item["id"]: item for item in client.get(base + "/folders").json()}
    assert folders[child["id"]]["path"] == "/renamed/child"
    assert client.delete(base + f"/folders/{root['id']}").status_code == 400
    moved = client.patch(base + f"/folders/{child['id']}", json={"parent_id": None})
    assert moved.status_code == 200 and moved.json()["path"] == "/child"
    assert client.delete(base + f"/folders/{child['id']}").status_code == 204
    with SessionLocal() as db:
        document = db.get(KnowledgeDocument, "folder-document")
        assert document is not None and document.folder_id is None
    assert client.delete(base + f"/folders/{root['id']}").status_code == 204


def test_tag_rename_duplicates_and_delete_unlink_documents(client):
    from app.core.database import SessionLocal

    base = _setup(client)
    with SessionLocal() as db:
        db.add(KnowledgeDocument(id="tag-document", knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                                source_path="tag-document/test.txt", original_filename="test.txt", file_hash="h", file_size=1))
        db.commit()
    first = client.post(base + "/tags", json={"name": "first", "color": "#ff0000"}).json()
    second = client.post(base + "/tags", json={"name": "second"}).json()
    assert client.put(base + "/documents/tag-document/tags", json={"tag_ids": [first["id"], second["id"]]}).status_code == 200
    assert client.patch(base + f"/tags/{first['id']}", json={"name": "second"}).status_code == 400
    renamed = client.patch(base + f"/tags/{first['id']}", json={"name": "renamed", "color": None})
    assert renamed.status_code == 200 and renamed.json()["color"] is None
    assert client.delete(base + f"/tags/{first['id']}").status_code == 204
    assert client.get(base + "/documents/tag-document/tags").json() == [second["id"]]
    assert client.get(base + "/documents/tag-document").status_code == 200


def test_folder_and_tag_updates_never_cross_knowledge_bases(client):
    base = _setup(client)
    folder = client.post(base + "/folders", json={"name": "private"}).json()
    tag = client.post(base + "/tags", json={"name": "private"}).json()
    other = client.post("/api/v1/knowledge-bases", json={"name": "Other", "slug": "other-kb"}).json()
    other_base = f"/api/v1/knowledge-bases/{other['id']}"
    for collection, item in [("folders", folder), ("tags", tag)]:
        assert client.patch(other_base + f"/{collection}/{item['id']}", json={"name": "stolen"}).status_code == 404
        assert client.delete(other_base + f"/{collection}/{item['id']}").status_code == 404
