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


def test_code_index_reparses_only_changed_files_and_reuses_other_facts(
    db_engine, tmp_path
):
    from app.adapters.local import LocalObjectStore
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.repositories.code_graph_repository import CodeGraphRepository
    from app.services.code_index_service import CodeIndexService
    from rag.code_graph.parser import CodeParser

    source = tmp_path / "incremental-source"
    source.mkdir()
    target_file = source / "target.py"
    caller_file = source / "caller.py"
    target_file.write_text("def target():\n    return 1\n", encoding="utf-8")
    caller_file.write_text(
        "from target import target\n\ndef caller():\n    return target()\n",
        encoding="utf-8",
    )

    class CountingParser:
        def __init__(self):
            self.delegate = CodeParser()
            self.paths = []

        def supported(self, path):
            return self.delegate.supported(path)

        def parse(self, path, source_text):
            self.paths.append(path)
            return self.delegate.parse(path, source_text)

    with SessionLocal() as db:
        service = CodeIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=LocalObjectStore(tmp_path / "incremental-objects"),
            task_queue=object(),
        )
        parser = CountingParser()
        service.parser = parser
        first = service.index_directory(source, revision="r1")
        parser.paths.clear()

        target_file.write_text("def target():\n    return 2\n", encoding="utf-8")
        second = service.index_directory(source, revision="r2")
        repository = CodeGraphRepository(db, DEFAULT_KNOWLEDGE_BASE_ID)
        caller = repository.search_symbols("caller", 5)[0]
        relations = repository.symbol_relations([caller["id"]], 20)

        assert first["files_parsed"] == 2
        assert second["files_parsed"] == 1
        assert second["files_reused"] == 1
        assert second["files_deleted"] == 0
        assert parser.paths == ["target.py"]
        assert any(
            item["relation_type"] == "CALLS"
            and item["target_name"].endswith("target")
            and item["resolution_status"] == "resolved"
            for item in relations
        )


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


def test_automatic_graph_build_is_configurable_and_deduplicated(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from app.services.graph_governance_service import GraphGovernanceService

    class Queue:
        def __init__(self):
            self.jobs = []

        def enqueue(self, job_id):
            self.jobs.append(job_id)

    queue = Queue()
    with SessionLocal() as db:
        db.add(KnowledgeBase.default())
        document = KnowledgeDocument(
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            source_path="auto.txt", original_filename="auto.txt",
            file_hash="auto-hash", file_size=10, status="ready",
        )
        db.add(document)
        db.flush()
        db.add(KnowledgeChunk(
            id="auto-chunk", knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            document_id=document.id, file_hash=document.file_hash, chunk_index=0,
            content="自动构图", content_hash="auto-content", token_count=4,
            graph_status="pending",
        ))
        db.commit()
        service = GraphGovernanceService(db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=queue)

        first = service.request_automatic_build("user", "document-job-1")
        second = service.request_automatic_build("user", "document-job-2")

        assert first is not None
        assert second is not None and second.id == first.id
        assert first.options_json["trigger"] == "document_index_completed"
        assert queue.jobs == [first.id]

        service.update_automation_settings(auto_build_enabled=False)
        first.status = "completed"
        db.commit()
        assert service.request_automatic_build("user", "document-job-3") is None


def test_relation_disambiguation_merge_combines_evidence_and_reverts(db_engine):
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
            source_path="relation-merge.txt", original_filename="relation-merge.txt",
            file_hash="relation-merge-hash", file_size=30, status="ready",
        )
        db.add(document)
        db.flush()
        triples = [
            ("relation-merge-1", "CALLS", "订单中心调用支付中心"),
            ("relation-merge-2", "USES", "订单中心使用支付中心"),
        ]
        for index, (chunk_id, _predicate, evidence) in enumerate(triples):
            db.add(KnowledgeChunk(
                id=chunk_id, knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                document_id=document.id, file_hash=document.file_hash, chunk_index=index,
                content=evidence, content_hash=f"hash-{index}", token_count=8,
                graph_status="completed",
            ))
        db.commit()
        store = SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID)
        for chunk_id, predicate, evidence in triples:
            store.upsert_extraction(
                {"document_id": document.id, "chunk_id": chunk_id},
                ChunkExtraction(
                    chunk_id=chunk_id,
                    entities=[
                        ExtractedEntity(local_id="a", name="订单中心", type="SYSTEM"),
                        ExtractedEntity(local_id="b", name="支付中心", type="SYSTEM"),
                    ],
                    relations=[ExtractedRelation(
                        subject="a", predicate=predicate, object="b",
                        evidence=evidence, confidence=0.9,
                    )],
                ),
            )
        subject_id = store.search_entities("订单中心", 1)[0]["id"]
        relations = store.get_neighbors([subject_id], 1, 10)
        by_predicate = {item["predicate"]: item for item in relations}
        governance = GraphGovernanceService(db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=object())
        merge = governance.merge_relations(
            by_predicate["CALLS"]["relation_id"],
            by_predicate["USES"]["relation_id"],
            "两条关系表达同一业务调用", "test-user",
        )
        merged = store.get_neighbors([subject_id], 1, 10)
        evidence = store.get_relation_evidence([by_predicate["CALLS"]["relation_id"]])

        assert [item["predicate"] for item in merged] == ["CALLS"]
        assert {item["quote"] for item in evidence} == {
            "订单中心调用支付中心", "订单中心使用支付中心",
        }
        assert governance.revert_relation_merge(merge.id, "test-user").status == "reverted"
        assert {item["predicate"] for item in store.get_neighbors([subject_id], 1, 10)} == {"CALLS", "USES"}
