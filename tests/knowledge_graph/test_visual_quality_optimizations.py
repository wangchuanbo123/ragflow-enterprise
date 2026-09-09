from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from rag.knowledge_graph.schemas import ChunkExtraction, ExtractedEntity


def test_schema_details_add_relation_endpoint_guidance():
    from app.services.graph_schema_induction_service import (
        _enrich_extraction_prompt,
        _schema_details,
    )

    entity_names = {"SERVICE", "DATABASE"}
    details = _schema_details(
        [{
            "name": "DEPENDS_ON",
            "description": "服务运行依赖数据库",
            "source_types": ["SERVICE", "UNKNOWN"],
            "target_types": ["DATABASE"],
            "direction": "source depends on target",
        }],
        {"DEPENDS_ON"},
        relation=True,
        endpoint_names=entity_names,
    )

    assert details[0]["source_types"] == ["SERVICE"]
    prompt = _enrich_extraction_prompt("只抽取明确事实。", [], details)
    assert "SERVICE → DATABASE" in prompt
    assert "source depends on target" in prompt


def test_extraction_rejects_relation_with_invalid_schema_endpoints():
    import json

    from rag.knowledge_graph.extractor import parse_extraction_response

    raw = json.dumps({
        "chunk_id": "constraint-chunk",
        "entities": [
            {"local_id": "service", "name": "订单服务", "type": "SERVICE"},
            {"local_id": "database", "name": "订单库", "type": "DATABASE"},
        ],
        "relations": [
            {
                "subject": "database",
                "predicate": "DEPENDS_ON",
                "object": "service",
                "evidence": "订单库依赖订单服务",
                "confidence": 0.9,
            },
        ],
    }, ensure_ascii=False)

    result = parse_extraction_response(
        raw,
        "constraint-chunk",
        "订单库依赖订单服务",
        {"SERVICE", "DATABASE"},
        {"DEPENDS_ON"},
        {"DEPENDS_ON": ({"SERVICE"}, {"DATABASE"})},
    )

    assert result.relations == []


def test_entity_name_reuses_unique_alias_in_same_type(db_engine):
    from app.core.database import SessionLocal
    from app.models.knowledge_base import KnowledgeBase
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore

    with SessionLocal() as db:
        if db.get(KnowledgeBase, DEFAULT_KNOWLEDGE_BASE_ID) is None:
            db.add(KnowledgeBase.default())
            db.flush()
        document = KnowledgeDocument(
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            source_path="alias.txt",
            original_filename="alias.txt",
            file_hash="alias-hash",
            file_size=20,
            status="ready",
        )
        db.add(document)
        db.flush()
        chunks = [
            KnowledgeChunk(
                id=f"alias-chunk-{index}",
                knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                document_id=document.id,
                file_hash=document.file_hash,
                chunk_index=index,
                content="订单服务也叫 OMS" if index == 0 else "OMS 负责订单处理",
                content_hash=f"alias-content-{index}",
                token_count=8,
            )
            for index in range(2)
        ]
        db.add_all(chunks)
        db.commit()

        store = SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID)
        store.upsert_extraction(
            {"document_id": document.id, "chunk_id": chunks[0].id},
            ChunkExtraction(
                chunk_id=chunks[0].id,
                entities=[ExtractedEntity(
                    local_id="service",
                    name="订单服务",
                    type="MODULE",
                    aliases=["OMS"],
                )],
            ),
        )
        store.upsert_extraction(
            {"document_id": document.id, "chunk_id": chunks[1].id},
            ChunkExtraction(
                chunk_id=chunks[1].id,
                entities=[ExtractedEntity(
                    local_id="service",
                    name="OMS",
                    type="MODULE",
                )],
            ),
        )

        assert store.stats()["entities"] == 1
        assert store.stats()["mentions"] == 2
        assert store.search_entities("OMS", 5)[0]["canonical_name"] == "订单服务"
