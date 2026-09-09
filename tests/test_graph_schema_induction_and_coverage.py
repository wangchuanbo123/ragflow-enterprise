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


def _seed_multi_document(db, *, count: int = 12, chunk_chars: int = 1200):
    """Seed multiple ready documents whose combined samples exceed the budget.

    每篇 3 个 chunk、每个 chunk chunk_chars 字符。12 篇 × 3 chunk × 1200 字符
    远超旧实现的 40000 字符总预算，用于验证预算均摊后每篇文档仍有样本进入提示词。
    """
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument

    if db.get(KnowledgeBase, DEFAULT_KNOWLEDGE_BASE_ID) is None:
        db.add(KnowledgeBase.default())
        db.flush()

    documents = []
    for doc_index in range(count):
        suffix = f"multi-{doc_index}"
        document = KnowledgeDocument(
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            source_path=f"{suffix}.txt",
            original_filename=f"{suffix}.txt",
            file_hash=f"hash-{suffix}",
            file_size=100,
            status="ready",
        )
        db.add(document)
        db.flush()
        for chunk_index in range(3):
            db.add(KnowledgeChunk(
                id=f"{suffix}-{chunk_index}",
                knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                document_id=document.id,
                file_hash=document.file_hash,
                chunk_index=chunk_index,
                content=(f"{suffix} 第{chunk_index}段 " + "领域内容" * (chunk_chars // 4)),
                content_hash=f"content-{suffix}-{chunk_index}",
                token_count=100,
                graph_status="completed",
                graph_schema_version=1,
            ))
        documents.append(document)
    db.commit()
    return documents


def test_schema_sampling_spreads_budget_across_all_documents(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.graph_schema_induction_service import GraphSchemaInductionService

    llm = _SchemaLLM()
    with SessionLocal() as db:
        documents = _seed_multi_document(db, count=12)
        document_ids = [document.id for document in documents]

        draft = GraphSchemaInductionService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, llm=llm
        ).generate_draft(created_by=None, document_ids=document_ids)

        prompt = llm.prompts[0]
        missing = [
            document.original_filename
            for document in documents
            if document.original_filename not in prompt
        ]
        assert not missing, f"这些文档没有进入采样提示词: {missing}"
        assert draft.generation_summary_json["source_documents"] == 12
        assert draft.generation_summary_json["sampled_documents"] == 12
        assert draft.generation_summary_json["sampled_characters"] <= 40000


def test_schema_induction_retries_after_invalid_json(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.graph_schema_induction_service import GraphSchemaInductionService

    class FlakyLLM:
        def __init__(self):
            self.calls = 0
            self.prompts: list[str] = []

        def generate(self, prompt: str) -> str:
            self.prompts.append(prompt)
            self.calls += 1
            if self.calls == 1:
                return "不是 JSON 的输出<think>思考中</think>"
            return json.dumps({
                "entity_types": ["ORDER_SERVICE"],
                "relation_types": ["CALLS"],
                "extraction_prompt": "重试成功。",
                "rationale": "第一次输出无效。",
            }, ensure_ascii=False)

    llm = FlakyLLM()
    with SessionLocal() as db:
        document, _ = _seed_document(db, suffix="retry")
        draft = GraphSchemaInductionService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, llm=llm
        ).generate_draft(created_by=None, document_ids=[document.id])

        assert llm.calls == 2
        assert draft.entity_types_json == ["ORDER_SERVICE"]
        assert "上一次输出解析失败" in llm.prompts[1]


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


def test_default_active_schema_uses_next_version_and_is_unique(db_engine):
    from sqlalchemy.exc import IntegrityError

    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.graph_governance import GraphSchemaDefinition
    from app.services.graph_governance_service import GraphGovernanceService

    with SessionLocal() as db:
        _seed_document(db, suffix="active-unique")
        governance = GraphGovernanceService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=object()
        )
        draft = governance.create_schema(
            entity_types=["SYSTEM", "MODULE", "DOCUMENT"],
            relation_types=["USES", "CALLS", "CONTAINS"],
            normalization_rules={},
            extraction_prompt=None,
            created_by=None,
        )
        active = governance.active_schema()

        assert draft.version == 1
        assert active.version == 2
        assert db.query(GraphSchemaDefinition).filter_by(status="active").count() == 1

        db.add(GraphSchemaDefinition(
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            version=3,
            status="active",
            entity_types_json=["SYSTEM"],
            relation_types_json=["USES"],
        ))
        try:
            db.commit()
            assert False, "database must reject a second active Schema"
        except IntegrityError:
            db.rollback()


def test_schema_generation_can_overwrite_draft_without_new_version(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.graph_governance import GraphSchemaDefinition
    from app.services.graph_schema_induction_service import GraphSchemaInductionService

    with SessionLocal() as db:
        document, _ = _seed_document(db, suffix="overwrite")
        service = GraphSchemaInductionService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, llm=_SchemaLLM()
        )
        draft = service.generate_draft(
            created_by=None, document_ids=[document.id]
        )
        original_version = draft.version
        original_count = db.query(GraphSchemaDefinition).count()

        overwritten = service.generate_draft(
            created_by=None,
            document_ids=[document.id],
            instructions="覆盖原草稿",
            target_schema_id=draft.id,
        )

        assert overwritten.id == draft.id
        assert overwritten.version == original_version
        assert db.query(GraphSchemaDefinition).count() == original_count
        assert overwritten.generation_summary_json["source_documents"] == 1


def test_schema_drift_creates_draft_only_for_new_types(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.graph_governance import GraphSchemaDefinition
    from app.services.graph_governance_service import GraphGovernanceService
    from app.services.graph_schema_induction_service import GraphSchemaInductionService

    class NoDriftLLM:
        def generate(self, prompt):
            del prompt
            return json.dumps({
                "entity_types": ["SYSTEM", "MODULE", "DOCUMENT"],
                "relation_types": ["USES", "CALLS", "CONTAINS"],
                "extraction_prompt": "沿用活动领域定义。",
                "rationale": "没有出现新概念。",
            }, ensure_ascii=False)

    with SessionLocal() as db:
        document, _ = _seed_document(db, suffix="drift")
        governance = GraphGovernanceService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=object()
        )
        active = governance.active_schema()
        detected = GraphSchemaInductionService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, llm=_SchemaLLM()
        ).detect_drift(created_by=None, document_ids=[document.id])

        assert detected["status"] == "drift_detected"
        assert "ORDER_SERVICE" in detected["added_entity_types"]
        suggested = db.get(GraphSchemaDefinition, detected["generated_schema_id"])
        assert suggested.status == "draft"
        assert suggested.generation_method == "document_drift"
        assert governance.active_schema().id == active.id

        draft_count = db.query(GraphSchemaDefinition).filter_by(status="draft").count()
        no_drift = GraphSchemaInductionService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, llm=NoDriftLLM()
        ).detect_drift(created_by=None, document_ids=[document.id])
        assert no_drift["status"] == "no_drift"
        assert db.query(GraphSchemaDefinition).filter_by(status="draft").count() == draft_count


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
