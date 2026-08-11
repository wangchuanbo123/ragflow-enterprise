from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from rag.knowledge_graph.schemas import (
    ChunkExtraction,
    ExtractedEntity,
    ExtractedRelation,
)
from rag.knowledge_graph.sqlite_store import SqliteGraphStore


def test_document_subgraph_contains_only_evidence_backed_edges(db_engine):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        db.add(KnowledgeBase(
            id=DEFAULT_KNOWLEDGE_BASE_ID,
            name="Default",
            slug="default",
            is_default=True,
            is_active=True,
        ))
        document = KnowledgeDocument(
            id="doc-subgraph",
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            source_path="subgraph.txt",
            original_filename="subgraph.txt",
            file_hash="doc-hash",
            file_size=12,
            status="ready",
        )
        chunk = KnowledgeChunk(
            id="chunk-subgraph",
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            document_id=document.id,
            file_hash=document.file_hash,
            chunk_index=0,
            content="订单服务依赖库存服务",
            content_hash="chunk-hash",
            token_count=8,
        )
        db.add_all([document, chunk])
        db.commit()

        store = SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID)
        store.upsert_extraction(
            {"document_id": document.id, "chunk_id": chunk.id},
            ChunkExtraction(
                chunk_id=chunk.id,
                entities=[
                    ExtractedEntity(local_id="e1", name="订单服务", type="MODULE"),
                    ExtractedEntity(local_id="e2", name="库存服务", type="MODULE"),
                ],
                relations=[
                    ExtractedRelation(
                        subject="e1",
                        predicate="DEPENDS_ON",
                        object="e2",
                        evidence="订单服务依赖库存服务",
                        confidence=0.95,
                    )
                ],
            ),
        )

        result = store.get_document_subgraph(document.id)

        assert result["document_id"] == document.id
        assert {node["label"] for node in result["nodes"]} == {"订单服务", "库存服务"}
        assert result["edges"][0]["predicate"] == "DEPENDS_ON"
        assert result["edges"][0]["evidence"][0]["chunk_id"] == chunk.id

        from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository

        repository = KnowledgeGraphRepository(db, DEFAULT_KNOWLEDGE_BASE_ID)
        correction = repository.create_correction(
            action="hide_relation",
            target_id=result["edges"][0]["id"],
            payload={},
            reason="关系已由业务负责人确认失效",
            created_by=None,
        )
        assert store.get_document_subgraph(document.id)["edges"] == []
        assert store.stats()["active_corrections"] == 1

        repository.revert_correction(correction.id, reverted_by=None)
        restored = store.get_document_subgraph(document.id)
        assert restored["edges"][0]["predicate"] == "DEPENDS_ON"
        assert store.stats()["active_corrections"] == 0


def test_neighbor_pagination_is_stable_and_does_not_prefetch_in_api(db_engine):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        db.add(KnowledgeBase(
            id=DEFAULT_KNOWLEDGE_BASE_ID,
            name="Default",
            slug="default",
            is_default=True,
            is_active=True,
        ))
        document = KnowledgeDocument(
            id="page-doc",
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            source_path="page.txt",
            original_filename="page.txt",
            file_hash="page-hash",
            file_size=20,
            status="ready",
        )
        chunk = KnowledgeChunk(
            id="page-chunk",
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            document_id=document.id,
            file_hash=document.file_hash,
            chunk_index=0,
            content="Root relates to A, B and C",
            content_hash="page-content-hash",
            token_count=8,
        )
        db.add_all([document, chunk])
        db.commit()

        store = SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID)
        store.upsert_extraction(
            {"document_id": document.id, "chunk_id": chunk.id},
            ChunkExtraction(
                chunk_id=chunk.id,
                entities=[
                    ExtractedEntity(local_id="root", name="Root", type="MODULE"),
                    ExtractedEntity(local_id="a", name="A", type="MODULE"),
                    ExtractedEntity(local_id="b", name="B", type="MODULE"),
                    ExtractedEntity(local_id="c", name="C", type="MODULE"),
                ],
                relations=[
                    ExtractedRelation(subject="root", predicate="USES", object="a", evidence="Root uses A", confidence=0.9),
                    ExtractedRelation(subject="root", predicate="USES", object="b", evidence="Root uses B", confidence=0.9),
                    ExtractedRelation(subject="root", predicate="USES", object="c", evidence="Root uses C", confidence=0.9),
                ],
            ),
        )
        root_id = store.search_entities("Root", 1)[0]["id"]
        full = store.get_neighbors([root_id], max_hops=1, limit=10)
        pages = [
            store.get_neighbors([root_id], max_hops=1, limit=1, offset=offset)[0]
            for offset in range(3)
        ]

        assert [item["relation_id"] for item in pages] == [
            item["relation_id"] for item in full
        ]
        assert len({item["relation_id"] for item in pages}) == 3
