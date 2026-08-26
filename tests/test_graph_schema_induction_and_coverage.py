from __future__ import annotations

import json


class _SchemaLLM:
    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {
                "entity_types": ["ORDER_SERVICE", "PAYMENT_SERVICE", "BUSINESS_PROCESS"],
                "relation_types": ["CALLS", "DEPENDS_ON", "PART_OF"],
                "extraction_prompt": "仅抽取订单支付领域中原文明确说明的系统与流程。",
                "rationale": "样本文档描述了订单服务调用支付服务。",
            },
            ensure_ascii=False,
        )


def _seed_document(db, *, suffix: str = "one"):
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument

    if db.get(KnowledgeBase, DEFAULT_KNOWLEDGE_BASE_ID) is None:
        db.add(KnowledgeBase.default())
        db.flush()
    document = KnowledgeDocument(
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        source_path=f"schema-{suffix}.txt",
        original_filename=f"schema-{suffix}.txt",
        file_hash=f"schema-hash-{suffix}",
        file_size=100,
        status="ready",
    )
    db.add(document)
    db.flush()
    chunks = []
    for index, content in enumerate([
        "订单服务调用支付服务创建支付单。",
        "支付服务依赖风控流程完成交易校验。",
        "退款流程属于订单售后业务流程。",
    ]):
        chunk = KnowledgeChunk(
            id=f"schema-{suffix}-{index}",
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            document_id=document.id,
            file_hash=document.file_hash,
            chunk_index=index,
            content=content,
            content_hash=f"schema-content-{suffix}-{index}",
            token_count=12,
            graph_status="completed",
            graph_schema_version=1,
        )
        db.add(chunk)
        chunks.append(chunk)
    db.commit()
    return document, chunks


def test_document_schema_induction_creates_reviewable_draft_with_provenance(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.graph_schema_induction_service import GraphSchemaInductionService

    llm = _SchemaLLM()
    with SessionLocal() as db:
        document, _ = _seed_document(db)
        draft = GraphSchemaInductionService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, llm=llm
        ).generate_draft(
            created_by=None,
            document_ids=[document.id],
            instructions="重点识别跨系统调用",
        )

        assert draft.status == "draft"
        assert draft.generation_method == "document_llm"
        assert draft.source_document_ids_json == [document.id]
        assert draft.generation_summary_json["source_documents"] == 1
        assert draft.generation_summary_json["sampled_chunks"] == 3
        assert draft.entity_types_json == [
            "BUSINESS_PROCESS", "ORDER_SERVICE", "PAYMENT_SERVICE"
        ]
        assert "重点识别跨系统调用" in llm.prompts[0]


def test_schema_generation_request_is_deduplicated(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.graph_governance_service import GraphGovernanceService

    class Queue:
        def __init__(self):
            self.ids = []

        def enqueue(self, job_id):
            self.ids.append(job_id)

    queue = Queue()
    with SessionLocal() as db:
        document, _ = _seed_document(db, suffix="queue")
        service = GraphGovernanceService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=queue
        )
        first = service.request_schema_generation(
            requested_by="user", document_ids=[document.id]
        )
        second = service.request_schema_generation(
            requested_by="user", document_ids=[document.id]
        )

        assert first.id == second.id
        assert first.job_type == "schema_generation"
        assert queue.ids == [first.id]


def test_document_graph_coverage_separates_completion_from_facts(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.graph_governance_service import GraphGovernanceService
    from rag.knowledge_graph.schemas import ChunkExtraction, ExtractedEntity, ExtractedRelation
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore

    with SessionLocal() as db:
        document, chunks = _seed_document(db, suffix="coverage")
        chunks[2].graph_status = "pending"
        db.commit()
        SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID).upsert_extraction(
            {"document_id": document.id, "chunk_id": chunks[0].id},
            ChunkExtraction(
                chunk_id=chunks[0].id,
                entities=[
                    ExtractedEntity(local_id="a", name="订单服务", type="SYSTEM"),
                    ExtractedEntity(local_id="b", name="支付服务", type="SYSTEM"),
                ],
                relations=[
                    ExtractedRelation(
                        subject="a",
                        predicate="CALLS",
                        object="b",
                        evidence=chunks[0].content,
                        confidence=0.95,
                    )
                ],
            ),
        )

        report = GraphGovernanceService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=object()
        ).document_coverage(document.id)
        row = report["documents"][0]

        assert row["processing_rate"] == 0.6667
        assert row["fact_coverage_rate"] == 0.3333
        assert row["unique_entities"] == 2
        assert row["unique_relations"] == 1
        assert row["coverage_status"] == "processing"


def test_empty_affected_community_rebuild_keeps_existing_hierarchy(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.graph_governance_service import GraphGovernanceService
    from rag.knowledge_graph.schemas import ChunkExtraction, ExtractedEntity, ExtractedRelation
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore

    with SessionLocal() as db:
        document, chunks = _seed_document(db, suffix="community")
        SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID).upsert_extraction(
            {"document_id": document.id, "chunk_id": chunks[0].id},
            ChunkExtraction(
                chunk_id=chunks[0].id,
                entities=[
                    ExtractedEntity(local_id="a", name="订单服务", type="SYSTEM"),
                    ExtractedEntity(local_id="b", name="支付服务", type="SYSTEM"),
                ],
                relations=[ExtractedRelation(
                    subject="a", predicate="CALLS", object="b",
                    evidence=chunks[0].content, confidence=0.9,
                )],
            ),
        )
        governance = GraphGovernanceService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=object()
        )
        initial = governance.rebuild_communities()
        unchanged = governance.rebuild_communities(changed_entity_ids=[])

        assert initial == {"communities": 1, "entities": 2}
        assert unchanged == {"communities": 1, "entities": 2, "unchanged": True}
        assert len(governance.list_communities()) == 2
