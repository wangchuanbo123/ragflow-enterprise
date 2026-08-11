from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME
from app.models.knowledge_graph import KgRelation, KgRelationEvidence
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from rag.knowledge_graph.schemas import (
    ChunkExtraction,
    ExtractedEntity,
    ExtractedRelation,
)
from rag.knowledge_graph.sqlite_store import SqliteGraphStore


def test_graph_correction_api_applies_and_reverts_overlay(client):
    document_id = "api-correction-doc"
    client.post(
        "/api/v1/auth/login",
        json={
            "username": INITIAL_ADMIN_USERNAME,
            "password": INITIAL_ADMIN_PASSWORD,
        },
    )
    knowledge_base_id = next(
        item["id"]
        for item in client.get("/api/v1/knowledge-bases").json()
        if item["is_default"]
    )

    from app.core.database import SessionLocal

    with SessionLocal() as db:
        document = KnowledgeDocument(
            id=document_id,
            knowledge_base_id=knowledge_base_id,
            source_path="api-correction.txt",
            original_filename="api-correction.txt",
            file_hash="api-correction-hash",
            file_size=16,
            status="ready",
        )
        chunk = KnowledgeChunk(
            id="api-correction-chunk",
            knowledge_base_id=knowledge_base_id,
            document_id=document.id,
            file_hash=document.file_hash,
            chunk_index=0,
            content="服务甲依赖服务乙",
            content_hash="api-correction-content",
            token_count=8,
        )
        db.add_all([document, chunk])
        db.commit()
        SqliteGraphStore(db, knowledge_base_id).upsert_extraction(
            {"document_id": document.id, "chunk_id": chunk.id},
            ChunkExtraction(
                chunk_id=chunk.id,
                entities=[
                    ExtractedEntity(local_id="a", name="服务甲", type="MODULE"),
                    ExtractedEntity(local_id="b", name="服务乙", type="MODULE"),
                ],
                relations=[
                    ExtractedRelation(
                        subject="a",
                        predicate="DEPENDS_ON",
                        object="b",
                        evidence="服务甲依赖服务乙",
                        confidence=0.9,
                    )
                ],
            ),
        )

    subgraph_url = (
        f"/api/v1/knowledge-bases/{knowledge_base_id}"
        f"/documents/{document_id}/subgraph"
    )
    original_subgraph = client.get(subgraph_url).json()
    relation_id = original_subgraph["edges"][0]["id"]
    seed_id = original_subgraph["nodes"][0]["id"]
    expanded = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base_id}"
        f"/knowledge-graph/subgraph?entity_ids={seed_id}&limit=10"
    )
    assert expanded.status_code == 200
    assert expanded.json()["seed_entity_ids"] == [seed_id]
    assert expanded.json()["edges"][0]["id"] == relation_id
    created = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/knowledge-graph/corrections",
        json={
            "action": "hide_relation",
            "target_id": relation_id,
            "reason": "业务负责人确认该依赖已失效",
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "active"
    assert client.get(subgraph_url).json()["edges"] == []

    correction_id = created.json()["id"]
    reverted = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}"
        f"/knowledge-graph/corrections/{correction_id}/revert"
    )
    assert reverted.status_code == 200
    assert reverted.json()["status"] == "reverted"
    assert client.get(subgraph_url).json()["edges"][0]["predicate"] == "DEPENDS_ON"

    conflicted = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/knowledge-graph/corrections",
        json={
            "action": "hide_relation",
            "target_id": relation_id,
            "reason": "Retain a human decision across graph rebuilds",
        },
    )
    assert conflicted.status_code == 201

    with SessionLocal() as db:
        db.query(KgRelationEvidence).filter(
            KgRelationEvidence.relation_id == relation_id
        ).delete()
        db.query(KgRelation).filter(KgRelation.id == relation_id).delete()
        db.commit()

    reconciled = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}"
        "/knowledge-graph/corrections/reconcile"
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["conflicts"] == 1
    history = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/knowledge-graph/corrections"
    ).json()
    conflict = next(item for item in history if item["id"] == conflicted.json()["id"])
    assert conflict["status"] == "conflict"
    assert conflict["conflict_reason"]
