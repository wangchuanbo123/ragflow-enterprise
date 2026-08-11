from __future__ import annotations


def test_python_code_parser_emits_symbols_calls_and_dependencies():
    from rag.code_graph.parser import CodeParser

    parsed = CodeParser().parse(
        "pkg/service.py",
        """import json
class Service(BaseService):
    def run(self, value: str):
        return helper(value)

def helper(value):
    return json.loads(value)
""",
    )

    assert parsed.parser_backend == "python_ast"
    assert {(item.kind, item.name) for item in parsed.symbols} >= {
        ("FILE", "service.py"),
        ("CLASS", "Service"),
        ("METHOD", "run"),
        ("FUNCTION", "helper"),
    }
    assert any(item.relation_type == "EXTENDS" and item.target_qualified_name == "BaseService" for item in parsed.relations)
    assert any(item.relation_type == "CALLS" and item.target_qualified_name == "helper" for item in parsed.relations)
    assert any(item.relation_type == "IMPORTS" and item.target_qualified_name == "json" for item in parsed.relations)


def test_code_index_builds_and_atomically_reuses_snapshot(db_engine, tmp_path):
    from app.adapters.local import LocalObjectStore
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.repositories.code_graph_repository import CodeGraphRepository
    from app.services.code_index_service import CodeIndexService

    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text(
        "def target():\n    return 1\n\ndef caller():\n    return target()\n",
        encoding="utf-8",
    )

    with SessionLocal() as db:
        service = CodeIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=LocalObjectStore(tmp_path / "objects"),
            task_queue=object(),
        )
        first = service.index_directory(source, revision="r1")
        second = service.index_directory(source, revision="r1")
        repository = CodeGraphRepository(db, DEFAULT_KNOWLEDGE_BASE_ID)
        target = repository.search_symbols("target", 5)

        assert first["status"] == "completed"
        assert first["files"] == 1
        assert first["resolved_relations"] >= 1
        assert second["status"] == "unchanged"
        assert len(repository.list_snapshots()) == 1
        assert target[0]["path"] == "main.py"
        assert target[0]["start_line"] == 1


def test_graph_quality_merge_overlay_and_communities(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from app.services.graph_governance_service import GraphGovernanceService
    from rag.knowledge_graph.schemas import ChunkExtraction, ExtractedEntity, ExtractedRelation
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore

    with SessionLocal() as db:
        document = KnowledgeDocument(
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            source_path="governance.txt",
            original_filename="governance.txt",
            file_hash="governance-hash",
            file_size=20,
            status="ready",
        )
        db.add(document)
        db.flush()
        chunk = KnowledgeChunk(
            id="governance-chunk",
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            document_id=document.id,
            file_hash=document.file_hash,
            chunk_index=0,
            content="支付服务使用风控服务",
            content_hash="governance-content",
            token_count=10,
            graph_status="completed",
        )
        db.add(chunk)
        db.commit()
        store = SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID)
        store.upsert_extraction(
            {"document_id": document.id, "chunk_id": chunk.id},
            ChunkExtraction(
                chunk_id=chunk.id,
                entities=[
                    ExtractedEntity(local_id="a", name="支付服务", type="SYSTEM"),
                    ExtractedEntity(local_id="b", name="风控服务", type="SYSTEM"),
                ],
                relations=[
                    ExtractedRelation(
                        subject="a", predicate="USES", object="b",
                        evidence="支付服务使用风控服务", confidence=0.95,
                    )
                ],
            ),
        )
        entities = store.search_entities("服务", 10)
        by_name = {item["canonical_name"]: item for item in entities}
        governance = GraphGovernanceService(db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=object())
        report = governance.assess_quality()
        hierarchy = governance.rebuild_communities()
        merge = governance.merge_entities(
            by_name["支付服务"]["id"], by_name["风控服务"]["id"],
            "测试人工标准化", "test-user",
        )
        merged_search = store.search_entities("风控服务", 5)

        assert report.status == "ready"
        assert report.metrics_json["evidence_coverage"] == 1.0
        assert hierarchy == {"communities": 1, "entities": 2}
        assert governance.list_communities()[0]["level"] == 1
        assert merge.status == "active"
        assert merged_search[0]["id"] == by_name["支付服务"]["id"]
        assert governance.revert_merge(merge.id, "test-user").status == "reverted"
