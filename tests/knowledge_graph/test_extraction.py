"""知识图谱抽取、归一化和存储测试。"""

import json

from rag.knowledge_graph.extractor import parse_extraction_response, _strip_markdown_and_think
from rag.knowledge_graph.normalizer import normalize_name, normalize_alias


def test_strip_markdown_and_think():
    assert _strip_markdown_and_think("```json\n{}\n```") == "{}"
    assert _strip_markdown_and_think("<think>reasoning</think>\n{}") == "{}"
    assert _strip_markdown_and_think("```{}```") == "{}"


def test_normalize_name():
    assert normalize_name("  Hello  World ") == "hello world"
    assert normalize_name("任务调度模块") == "任务调度模块"
    assert normalize_name("　ABC　") == "abc"


def test_parse_valid_extraction():
    chunk_id = "c1"
    content = "任务调度模块依赖消息中间件进行通信"
    raw = json.dumps({
        "chunk_id": chunk_id,
        "entities": [
            {"local_id": "e1", "name": "任务调度模块", "type": "MODULE"},
            {"local_id": "e2", "name": "消息中间件", "type": "MODULE"},
        ],
        "relations": [
            {"subject": "e1", "predicate": "DEPENDS_ON", "object": "e2",
             "evidence": "任务调度模块依赖消息中间件", "confidence": 0.9},
        ],
    })

    result = parse_extraction_response(raw, chunk_id, content)
    assert len(result.entities) == 2
    assert len(result.relations) == 1
    assert result.relations[0].predicate == "DEPENDS_ON"


def test_parse_recovers_missing_local_ids_and_name_relation_endpoints():
    content = "订单服务是业务平台的一部分，订单服务负责处理订单超时。"
    raw = json.dumps({
        "entities": [
            {"name": "订单服务", "type": "service_component"},
            {"name": "业务平台", "type": "software_system"},
        ],
        "relations": [
            {
                "subject": "订单服务",
                "predicate": "part_of",
                "object": "业务平台",
                "evidence": "订单服务是业务平台的一部分",
            }
        ],
    }, ensure_ascii=False)

    result = parse_extraction_response(
        raw,
        "diagnostic-1",
        content,
        entity_types={"SERVICE_COMPONENT", "SOFTWARE_SYSTEM"},
        relation_types={"PART_OF"},
    )

    assert [entity.local_id for entity in result.entities] == ["e1", "e2"]
    assert [entity.type for entity in result.entities] == [
        "SERVICE_COMPONENT",
        "SOFTWARE_SYSTEM",
    ]
    assert result.relations[0].subject == "e1"
    assert result.relations[0].object == "e2"
    assert result.relations[0].confidence == 0.8


def test_parse_does_not_resolve_ambiguous_entity_names():
    content = "A依赖B"
    raw = json.dumps({
        "entities": [
            {"name": "服务", "type": "MODULE"},
            {"name": "服务", "type": "MODULE"},
            {"name": "B", "type": "MODULE"},
        ],
        "relations": [
            {
                "subject": "服务",
                "predicate": "DEPENDS_ON",
                "object": "B",
                "evidence": "A依赖B",
            }
        ],
    }, ensure_ascii=False)

    result = parse_extraction_response(raw, "c1", content)

    assert len(result.entities) == 3
    assert result.relations == []


def test_parse_rejects_unknown_entity_type():
    content = "测试内容"
    raw = json.dumps({
        "entities": [{"local_id": "e1", "name": "X", "type": "UNKNOWN_TYPE"}],
        "relations": [],
    })
    result = parse_extraction_response(raw, "c1", content)
    assert len(result.entities) == 0


def test_parse_rejects_unknown_predicate():
    content = "A和B相关"
    raw = json.dumps({
        "entities": [
            {"local_id": "e1", "name": "A", "type": "MODULE"},
            {"local_id": "e2", "name": "B", "type": "MODULE"},
        ],
        "relations": [
            {"subject": "e1", "predicate": "WRONG_PREDICATE", "object": "e2", "evidence": "A和B相关"},
        ],
    })
    result = parse_extraction_response(raw, "c1", content)
    assert len(result.relations) == 0


def test_parse_rejects_invalid_local_id():
    content = "内容"
    raw = json.dumps({
        "entities": [{"local_id": "e1", "name": "A", "type": "MODULE"}],
        "relations": [
            {"subject": "e1", "predicate": "DEPENDS_ON", "object": "e99", "evidence": "内容"},
        ],
    })
    result = parse_extraction_response(raw, "c1", content)
    assert len(result.relations) == 0


def test_parse_rejects_self_loop():
    content = "内容"
    raw = json.dumps({
        "entities": [{"local_id": "e1", "name": "A", "type": "MODULE"}],
        "relations": [
            {"subject": "e1", "predicate": "RELATED_TO", "object": "e1", "evidence": "内容"},
        ],
    })
    result = parse_extraction_response(raw, "c1", content)
    assert len(result.relations) == 0


def test_parse_rejects_evidence_not_in_content():
    content = "这是原文内容"
    raw = json.dumps({
        "entities": [
            {"local_id": "e1", "name": "A", "type": "MODULE"},
            {"local_id": "e2", "name": "B", "type": "MODULE"},
        ],
        "relations": [
            {"subject": "e1", "predicate": "DEPENDS_ON", "object": "e2",
             "evidence": "这段话不在原文中"},
        ],
    })
    result = parse_extraction_response(raw, "c1", content)
    assert len(result.relations) == 0


def test_sqlite_store_upsert_and_query(db_engine):
    """测试 SQLite 图谱存储。"""
    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from rag.knowledge_graph.schemas import ChunkExtraction, ExtractedEntity, ExtractedRelation
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore

    with SessionLocal() as db:
        doc = KnowledgeDocument(
            source_path="test.txt",
            original_filename="test.txt",
            file_hash="hash1",
            file_size=100,
            status="ready",
            embedding_provider="ollama",
            embedding_model="test",
        )
        db.add(doc)
        db.commit()

        chunk = KnowledgeChunk(
            id="chunk_id_1",
            document_id=doc.id,
            file_hash="hash1",
            chunk_index=0,
            content="任务调度模块依赖消息中间件",
            content_hash="ch1",
            token_count=10,
        )
        db.add(chunk)
        db.commit()

        store = SqliteGraphStore(db)

        extraction = ChunkExtraction(
            chunk_id="chunk_id_1",
            entities=[
                ExtractedEntity(local_id="e1", name="任务调度模块", type="MODULE", aliases=["调度模块"]),
                ExtractedEntity(local_id="e2", name="消息中间件", type="MODULE"),
            ],
            relations=[
                ExtractedRelation(subject="e1", predicate="DEPENDS_ON", object="e2",
                                  evidence="任务调度模块依赖消息中间件", confidence=0.9),
            ],
        )

        store.upsert_extraction(
            {"chunk_id": "chunk_id_1", "document_id": doc.id, "content": chunk.content},
            extraction,
        )

        stats = store.stats()
        assert stats["entities"] == 2
        assert stats["relations"] == 1
        assert stats["evidence"] == 1

        # Idempotent: running again should not duplicate
        store.upsert_extraction(
            {"chunk_id": "chunk_id_1", "document_id": doc.id, "content": chunk.content},
            extraction,
        )
        stats = store.stats()
        assert stats["entities"] == 2
        assert stats["relations"] == 1
        assert stats["evidence"] == 1

        # Search entities
        results = store.search_entities("任务调度", 10)
        assert len(results) >= 1
        assert results[0]["canonical_name"] == "任务调度模块"
        question_results = store.search_entities("任务调度模块依赖什么", 10)
        assert question_results[0]["canonical_name"] == "任务调度模块"
        alias_results = store.search_entities("调度模块有哪些依赖", 10)
        assert alias_results[0]["canonical_name"] == "任务调度模块"

        # Get neighbors
        entity_id = results[0]["id"]
        neighbors = store.get_neighbors([entity_id], max_hops=2, limit=10)
        assert len(neighbors) >= 1
        assert neighbors[0]["predicate"] == "DEPENDS_ON"

        # Delete document graph
        store.delete_document_graph(doc.id)
        store.cleanup_orphans()
        stats = store.stats()
        # Entities might be cleaned up if no mentions remain
        assert stats["mentions"] == 0
        assert stats["evidence"] == 0


def test_batch_extractor_uses_one_llm_call():
    from rag.knowledge_graph.extractor import GraphExtractor

    class FakeBatchLLM:
        def __init__(self):
            self.calls = 0

        def generate(self, prompt):
            self.calls += 1
            assert '"chunk_id": "c1"' in prompt
            assert '"chunk_id": "c2"' in prompt
            return json.dumps(
                {
                    "chunks": [
                        {
                            "chunk_id": "c1",
                            "entities": [
                                {"local_id": "e1", "name": "A", "type": "MODULE"},
                                {"local_id": "e2", "name": "B", "type": "MODULE"},
                            ],
                            "relations": [
                                {
                                    "subject": "e1",
                                    "predicate": "USES",
                                    "object": "e2",
                                    "evidence": "A uses B",
                                    "confidence": 0.9,
                                }
                            ],
                        },
                        {
                            "chunk_id": "c2",
                            "entities": [],
                            "relations": [],
                        },
                    ]
                }
            )

    llm = FakeBatchLLM()
    extractor = GraphExtractor(llm=llm, max_retries=0)
    results = extractor.extract_batch(
        [
            {"chunk_id": "c1", "content": "A uses B"},
            {"chunk_id": "c2", "content": "Nothing explicit"},
        ]
    )

    assert llm.calls == 1
    assert [result.chunk_id for result in results] == ["c1", "c2"]
    assert len(results[0].relations) == 1


def test_batch_extractor_retries_only_missing_chunk_ids():
    from rag.knowledge_graph.extractor import GraphExtractor

    class PartialBatchLLM:
        def __init__(self):
            self.calls = 0
            self.prompts: list[str] = []

        def generate(self, prompt):
            self.calls += 1
            self.prompts.append(prompt)
            chunk_id = "c1" if self.calls == 1 else "c2"
            return json.dumps({
                "chunks": [{
                    "chunk_id": chunk_id,
                    "entities": [{
                        "name": "订单服务" if chunk_id == "c1" else "支付服务",
                        "type": "MODULE",
                    }],
                    "relations": [],
                }]
            }, ensure_ascii=False)

    llm = PartialBatchLLM()
    extractor = GraphExtractor(llm=llm, max_retries=1)
    results = extractor.extract_batch([
        {"chunk_id": "c1", "content": "订单服务处理订单"},
        {"chunk_id": "c2", "content": "支付服务处理支付"},
    ])

    assert llm.calls == 2
    assert [result.chunk_id for result in results] == ["c1", "c2"]
    assert '"chunk_id": "c1"' not in llm.prompts[1]
    assert '"chunk_id": "c2"' in llm.prompts[1]


def test_graph_build_reextracts_old_schema_and_filters_low_confidence(
    db_engine,
):
    from app.core.config import GRAPH_SCHEMA_VERSION
    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from app.services.knowledge_graph_service import GraphBuildService
    from rag.knowledge_graph.schemas import (
        ChunkExtraction,
        ExtractedEntity,
        ExtractedRelation,
    )

    class FakeBatchExtractor:
        def __init__(self):
            self.calls = 0

        def extract_batch(self, chunks):
            self.calls += 1
            return [
                ChunkExtraction(
                    chunk_id=chunks[0]["chunk_id"],
                    entities=[
                        ExtractedEntity(
                            local_id="e1",
                            name="A",
                            type="MODULE",
                        ),
                        ExtractedEntity(
                            local_id="e2",
                            name="B",
                            type="MODULE",
                        ),
                    ],
                    relations=[
                        ExtractedRelation(
                            subject="e1",
                            predicate="USES",
                            object="e2",
                            evidence="Module A uses module B to satisfy the requirement",
                            confidence=0.2,
                        )
                    ],
                )
            ]

    with SessionLocal() as db:
        doc = KnowledgeDocument(
            source_path="graph.txt",
            original_filename="graph.txt",
            file_hash="hash",
            file_size=10,
            status="ready",
            embedding_provider="ollama",
            embedding_model="test",
        )
        db.add(doc)
        db.commit()
        chunk = KnowledgeChunk(
            id="graph-chunk",
            document_id=doc.id,
            file_hash="hash",
            chunk_index=0,
            content="Module A uses module B to satisfy the requirement.",
            content_hash="content-hash",
            token_count=8,
            graph_status="completed",
            graph_schema_version=0,
        )
        db.add(chunk)
        db.commit()

        extractor = FakeBatchExtractor()
        result = GraphBuildService(db, extractor=extractor).build_graph()

        db.refresh(chunk)
        assert extractor.calls == 1
        assert result["succeeded"] == 1
        assert result["graph_stats"]["relations"] == 0
        # The effective extraction contract uses the newest version across the
        # general Schema and four independent professional Schemas.
        assert chunk.graph_schema_version >= GRAPH_SCHEMA_VERSION


def test_graph_build_recovers_interrupted_processing_chunk(db_engine):
    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from app.services.knowledge_graph_service import GraphBuildService
    from rag.knowledge_graph.schemas import ChunkExtraction

    class EmptyExtractor:
        def extract_batch(self, chunks):
            return [
                ChunkExtraction(chunk_id=chunk["chunk_id"], entities=[], relations=[])
                for chunk in chunks
            ]

    with SessionLocal() as db:
        doc = KnowledgeDocument(
            source_path="interrupted.txt",
            original_filename="interrupted.txt",
            file_hash="hash",
            file_size=10,
            status="ready",
            embedding_provider="ollama",
            embedding_model="test",
        )
        db.add(doc)
        db.commit()
        chunk = KnowledgeChunk(
            id="interrupted-chunk",
            document_id=doc.id,
            file_hash="hash",
            chunk_index=0,
            content="This section describes services, but no graph facts exist here.",
            content_hash="content-hash",
            token_count=10,
            graph_status="processing",
        )
        db.add(chunk)
        db.commit()

        result = GraphBuildService(db, extractor=EmptyExtractor()).build_graph()

        db.refresh(chunk)
        assert result["recovered_processing"] == 1
        assert result["succeeded"] == 1
        assert result["empty_extractions"] == 1
        assert chunk.graph_status == "completed"
        assert chunk.graph_error is None


def test_graph_extractor_uses_custom_active_schema_types():
    from rag.knowledge_graph.extractor import GraphExtractor

    class SchemaAwareLLM:
        def __init__(self):
            self.prompt = ""

        def generate(self, prompt):
            self.prompt = prompt
            return (
                '{"chunks":[{"chunk_id":"custom-1","entities":['
                '{"local_id":"e1","name":"订单中心","type":"BUSINESS_SERVICE"},'
                '{"local_id":"e2","name":"支付中心","type":"BUSINESS_SERVICE"}],'
                '"relations":[{"subject":"e1","predicate":"ROUTES_TO",'
                '"object":"e2","evidence":"订单中心路由到支付中心","confidence":0.95}]}]}'
            )

    llm = SchemaAwareLLM()
    extractor = GraphExtractor(llm=llm, max_retries=0)
    extractor.configure_schema(
        entity_types=["BUSINESS_SERVICE"],
        relation_types=["ROUTES_TO"],
        prompt_prefix="只抽取生产系统之间的调用关系。",
    )
    result = extractor.extract_batch([{
        "chunk_id": "custom-1",
        "content": "订单中心路由到支付中心",
    }])

    assert result[0].entities[0].type == "BUSINESS_SERVICE"
    assert result[0].relations[0].predicate == "ROUTES_TO"
    assert "BUSINESS_SERVICE" in llm.prompt
    assert "ROUTES_TO" in llm.prompt
    assert llm.prompt.startswith("只抽取生产系统之间的调用关系。")
