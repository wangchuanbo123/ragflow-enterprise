from __future__ import annotations


class _Queue:
    def __init__(self):
        self.job_ids: list[str] = []

    def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)


def _source(db):
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument

    document = KnowledgeDocument(
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        source_path="manual-governance.txt",
        original_filename="manual-governance.txt",
        file_hash="manual-governance-file",
        file_size=100,
        status="ready",
    )
    db.add(document)
    db.flush()
    chunk = KnowledgeChunk(
        id="manual-governance-chunk",
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        document_id=document.id,
        file_hash=document.file_hash,
        chunk_index=0,
        content="订单超时由订单调度器处理，并通知告警中心。",
        content_hash="manual-governance-content",
        token_count=18,
        graph_status="completed",
    )
    db.add(chunk)
    db.commit()
    return document, chunk


def test_manual_entity_and_relation_have_real_chunk_and_refresh_jobs(db_engine):
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.index_job import IndexJob
    from app.models.knowledge_graph import KgEntity, KgEntityMention
    from app.services.graph_governance_service import GraphGovernanceService
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore

    with SessionLocal() as db:
        document, chunk = _source(db)
        queue = _Queue()
        service = GraphGovernanceService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=queue
        )
        service.active_schema()
        order = service.create_correction(
            action="add_entity",
            target_id=None,
            payload={
                "canonical_name": "订单调度器",
                "entity_type": "SYSTEM",
                "description": "处理订单调度和超时",
                "document_id": document.id,
                "chunk_id": chunk.id,
                "evidence": "订单调度器",
            },
            reason="补充文档中明确存在的组件",
            created_by="test-user",
        )
        alert = service.create_correction(
            action="add_entity",
            target_id=None,
            payload={
                "canonical_name": "告警中心",
                "entity_type": "SYSTEM",
                "document_id": document.id,
                "chunk_id": chunk.id,
                "evidence": "告警中心",
            },
            reason="补充文档中明确存在的组件",
            created_by="test-user",
        )
        relation = service.create_correction(
            action="add_relation",
            target_id=None,
            payload={
                "subject_entity_id": order.target_id,
                "predicate": "CALLS",
                "object_entity_id": alert.target_id,
                "document_id": document.id,
                "chunk_id": chunk.id,
                "evidence": "并通知告警中心",
            },
            reason="补充人工确认的调用关系",
            created_by="test-user",
        )

        store = SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID)
        effective = store.get_relations([f"manual:{relation.id}"])
        evidence = store.get_relation_evidence([f"manual:{relation.id}"])
        entity_evidence = store.get_entity_evidence([order.target_id])
        job_types = set(db.scalars(select(IndexJob.job_type)))

        assert db.get(KgEntity, order.target_id).canonical_name == "订单调度器"
        assert db.scalar(select(KgEntityMention).where(
            KgEntityMention.entity_id == order.target_id
        )).chunk_id == chunk.id
        assert effective[0]["predicate"] == "CALLS"
        assert evidence[0]["chunk_id"] == chunk.id
        assert entity_evidence[0]["chunk_id"] == chunk.id
        assert job_types == {
            "entity_vector_rebuild", "relation_vector_rebuild", "graph_maintenance"
        }
        assert len(queue.job_ids) == 3


def test_manual_relation_can_be_updated_hidden_and_reverted(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.graph_governance_service import GraphGovernanceService
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore

    with SessionLocal() as db:
        document, chunk = _source(db)
        service = GraphGovernanceService(db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=object())
        service.active_schema()
        entity_ids = []
        for name in ("订单调度器", "告警中心"):
            item = service.create_correction(
                action="add_entity", target_id=None,
                payload={
                    "canonical_name": name, "entity_type": "SYSTEM",
                    "document_id": document.id, "chunk_id": chunk.id,
                    "evidence": name,
                },
                reason="补充文档实体", created_by="test-user",
            )
            entity_ids.append(item.target_id)
        relation = service.create_correction(
            action="add_relation", target_id=None,
            payload={
                "subject_entity_id": entity_ids[0], "predicate": "CALLS",
                "object_entity_id": entity_ids[1], "document_id": document.id,
                "chunk_id": chunk.id, "evidence": "并通知告警中心",
            },
            reason="补充关系", created_by="test-user",
        )
        relation_id = f"manual:{relation.id}"
        store = SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID)

        update = service.create_correction(
            action="update_relation", target_id=relation_id,
            payload={"predicate": "USES", "confidence": 0.8},
            reason="调整为更准确的关系", created_by="test-user",
        )
        assert store.get_relations([relation_id])[0]["predicate"] == "USES"

        hidden = service.create_correction(
            action="hide_relation", target_id=relation_id, payload={},
            reason="暂时隐藏该关系", created_by="test-user",
        )
        assert store.get_relations([relation_id]) == []
        service.revert_correction(hidden.id, "test-user")
        assert store.get_relations([relation_id])[0]["predicate"] == "USES"
        service.revert_correction(update.id, "test-user")
        assert store.get_relations([relation_id])[0]["predicate"] == "CALLS"


def test_manual_evidence_must_belong_to_selected_chunk(db_engine):
    import pytest

    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository

    with SessionLocal() as db:
        document, chunk = _source(db)
        with pytest.raises(ValueError, match="证据必须是所选 Chunk"):
            KnowledgeGraphRepository(db, DEFAULT_KNOWLEDGE_BASE_ID).create_correction(
                action="add_entity", target_id=None,
                payload={
                    "canonical_name": "不存在的实体", "entity_type": "SYSTEM",
                    "document_id": document.id, "chunk_id": chunk.id,
                    "evidence": "这段文字不在原文中",
                },
                reason="验证来源约束", created_by="test-user",
            )
