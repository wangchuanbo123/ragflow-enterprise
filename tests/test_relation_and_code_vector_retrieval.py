from __future__ import annotations

from types import SimpleNamespace
import json

from langchain_core.documents import Document


class _FakeEmbedding:
    def identity(self):
        from rag.providers.interfaces import ProviderIdentity

        return ProviderIdentity(
            kind="embedding",
            provider="fake",
            model="fake-embedding",
        )

    def get_model(self):
        return self


class _FakeVectorStore:
    def __init__(self):
        self.documents: list[Document] = []
        self.ids: list[str] = []
        self.vector_query_count = 0

    def add_documents(self, documents, ids=None):
        for document, vector_id in zip(documents, ids or []):
            if vector_id in self.ids:
                position = self.ids.index(vector_id)
                self.documents[position] = document
            else:
                self.documents.append(document)
                self.ids.append(vector_id)

    def delete(self, ids):
        removed = set(ids)
        kept = [
            (vector_id, document)
            for vector_id, document in zip(self.ids, self.documents)
            if vector_id not in removed
        ]
        self.ids = [item[0] for item in kept]
        self.documents = [item[1] for item in kept]

    def similarity_search_with_relevance_scores(self, query, k):
        del query
        return [
            (document, 1.0 / (index + 1))
            for index, document in enumerate(self.documents[:k])
        ]

    def similarity_search_by_vector_with_relevance_scores(self, embedding, k):
        del embedding
        self.vector_query_count += 1
        return [
            (document, 1.0 / (index + 1))
            for index, document in enumerate(self.documents[:k])
        ]


class _FakeVectorProvider:
    def __init__(self):
        self.indexes: dict[str, _FakeVectorStore] = {}

    def open(self, embedding, index_ref):
        del embedding
        return self.indexes.setdefault(index_ref.name, _FakeVectorStore())

    def delete_index(self, index_ref):
        self.indexes.pop(index_ref.name, None)

    def delete_vectors(self, index_ref, *, ids=None, where=None):
        del where
        self.indexes[index_ref.name].delete(ids or [])


class _FakeTaskQueue:
    def __init__(self):
        self.job_ids = []

    def enqueue(self, job_id):
        self.job_ids.append(job_id)


class _FakeSummaryLLM:
    def __init__(self):
        self.generated_symbol_count = 0

    def identity(self):
        from rag.providers.interfaces import ProviderIdentity

        return ProviderIdentity(
            kind="llm",
            provider="fake",
            model="fake-code-summary",
        )

    def generate(self, prompt):
        items = json.loads(prompt.rsplit("符号：", 1)[1])
        self.generated_symbol_count += len(items)
        return json.dumps([{
            "stable_key": item["stable_key"],
            "summary": (
                "处理订单超时，取消订单并释放库存。"
                if "timeout" in item["qualified_name"]
                else f"提供 {item['qualified_name']} 对应的代码能力。"
            ),
        } for item in items], ensure_ascii=False)


def _container(summary_llm=None):
    values = dict(
        embedding=_FakeEmbedding(),
        vector_store=_FakeVectorProvider(),
        task_queue=_FakeTaskQueue(),
    )
    if summary_llm is not None:
        values["extraction_llm"] = summary_llm
    return SimpleNamespace(**values)


def _build_relation_graph(db):
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from rag.knowledge_graph.schemas import (
        ChunkExtraction,
        ExtractedEntity,
        ExtractedRelation,
    )
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore

    document = KnowledgeDocument(
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        source_path="architecture.md",
        original_filename="architecture.md",
        file_hash="relation-file-hash",
        file_size=32,
        status="ready",
    )
    db.add(document)
    db.flush()
    chunk = KnowledgeChunk(
        id="relation-retrieval-chunk",
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        document_id=document.id,
        file_hash=document.file_hash,
        chunk_index=0,
        content="订单服务依赖库存服务。",
        content_hash="relation-content-hash",
        token_count=12,
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
                ExtractedEntity(local_id="order", name="订单服务", type="SYSTEM"),
                ExtractedEntity(local_id="stock", name="库存服务", type="SYSTEM"),
            ],
            relations=[
                ExtractedRelation(
                    subject="order",
                    predicate="DEPENDS_ON",
                    object="stock",
                    evidence="订单服务依赖库存服务",
                    confidence=0.96,
                )
            ],
        ),
    )
    return store


def test_relation_lexical_seed_retrieves_evidence_without_entity_name(
    db_engine, monkeypatch
):
    from app.bootstrap import container as container_module
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from rag.knowledge_graph.retriever import KnowledgeGraphRetriever

    with SessionLocal() as db:
        store = _build_relation_graph(db)
        matches = store.search_relations("有哪些依赖关系？", 5)
        assert matches[0]["predicate"] == "DEPENDS_ON"
        assert matches[0]["match_method"] == "relation_lexical"

        provider_container = _container()
        provider_container.graph_store = lambda scoped_db, knowledge_base_id=None: store
        monkeypatch.setattr(
            container_module, "get_provider_container", lambda: provider_container
        )
        documents = KnowledgeGraphRetriever(
            db=db, knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID
        ).get_relevant_documents("有哪些依赖关系？", top_k=5)

        assert documents[0].metadata["chunk_id"] == "relation-retrieval-chunk"
        assert documents[0].metadata["graph_facts"] == [
            "订单服务 --DEPENDS_ON--> 库存服务"
        ]


def test_relation_vectors_are_versioned_and_searchable(db_engine, monkeypatch):
    from app.bootstrap import container as container_module
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.relation_vector_service import RelationVectorService
    from rag.knowledge_graph.retriever import KnowledgeGraphRetriever

    with SessionLocal() as db:
        store = _build_relation_graph(db)
        container = _container()
        service = RelationVectorService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, container=container
        )

        result = service.rebuild()
        semantic = service.semantic_search(
            "上游服务需要哪些下游能力",
            5,
            query_embedding=[0.1, 0.2],
        )

        assert result["status"] == "ready"
        assert result["relation_count"] == 1
        assert result["index_name"].startswith("kg_relations_")
        assert semantic[0]["predicate"] == "DEPENDS_ON"
        assert next(iter(container.vector_store.indexes.values())).vector_query_count == 1
        assert "依赖" in next(iter(container.vector_store.indexes.values())).documents[0].page_content
        assert service.rebuild()["status"] == "unchanged"

        container.graph_store = lambda scoped_db, knowledge_base_id=None: store
        monkeypatch.setattr(
            container_module, "get_provider_container", lambda: container
        )
        documents = KnowledgeGraphRetriever(
            db=db, knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID
        ).get_relevant_documents("上游能力之间是什么约束？", top_k=5)
        assert documents[0].metadata["chunk_id"] == "relation-retrieval-chunk"


def test_code_symbols_are_vectorized_and_vector_only_hits_are_hydrated(
    db_engine, tmp_path
):
    from app.adapters.local import LocalObjectStore
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.code_index_service import CodeIndexService
    from app.services.code_vector_service import CodeVectorService

    source = tmp_path / "source"
    source.mkdir()
    (source / "service.py").write_text(
        "def calculate_stock(item_id: str):\n    return item_id\n",
        encoding="utf-8",
    )

    with SessionLocal() as db:
        CodeIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=LocalObjectStore(tmp_path / "objects"),
            task_queue=_FakeTaskQueue(),
        ).index_directory(source, revision="vector-test")
        container = _container()
        service = CodeVectorService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, container=container, summary_agent_enabled=False
        )

        result = service.rebuild()
        matches = service.hybrid_search(
            "处理库存计算的业务入口",
            5,
            query_embedding=[0.1, 0.2],
        )
        fused_matches = service.hybrid_search("calculate_stock", 5)

        assert result["status"] == "ready"
        assert result["symbol_count"] >= 1
        assert result["index_name"].startswith("code_symbols_")
        assert matches
        assert next(iter(container.vector_store.indexes.values())).vector_query_count == 1
        assert matches[0]["path"] == "service.py"
        assert matches[0]["retrieval_methods"] == ["vector"]
        target = next(
            item for item in fused_matches
            if item["qualified_name"].endswith("calculate_stock")
        )
        assert target["retrieval_methods"] == ["lexical", "vector"]
        assert service.state().snapshot_id == result["snapshot_id"]
        vector_documents = next(iter(container.vector_store.indexes.values())).documents
        assert any("calculate_stock" in document.page_content for document in vector_documents)


def test_code_vectors_incrementally_update_changed_symbols(db_engine, tmp_path):
    from app.adapters.local import LocalObjectStore
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.code_index_service import CodeIndexService
    from app.services.code_vector_service import CodeVectorService

    source = tmp_path / "vector-incremental-source"
    source.mkdir()
    changed_file = source / "changed.py"
    stable_file = source / "stable.py"
    changed_file.write_text("def changed():\n    return 1\n", encoding="utf-8")
    stable_file.write_text("def stable():\n    return 10\n", encoding="utf-8")

    with SessionLocal() as db:
        index_service = CodeIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=LocalObjectStore(tmp_path / "vector-incremental-objects"),
            task_queue=_FakeTaskQueue(),
        )
        container = _container()
        vector_service = CodeVectorService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, container=container, summary_agent_enabled=False
        )
        index_service.index_directory(source, revision="r1")
        first = vector_service.rebuild()
        vector_store = container.vector_store.indexes[first["index_name"]]
        stable_vector_id = next(
            vector_id
            for vector_id, document in zip(vector_store.ids, vector_store.documents)
            if document.metadata["qualified_name"].endswith("stable")
        )

        changed_file.write_text("def changed():\n    return 2\n", encoding="utf-8")
        index_service.index_directory(source, revision="r2")
        second = vector_service.rebuild()
        matches = vector_service.semantic_search("stable", 20)

        assert second["index_name"] == first["index_name"]
        assert second["symbols_reused"] >= 2
        assert second["symbols_added_or_updated"] >= 1
        assert second["symbols_deleted"] == 0
        assert stable_vector_id in vector_store.ids
        assert len(vector_store.ids) == len(set(vector_store.ids))
        assert any(item["qualified_name"].endswith("stable") for item in matches)
        assert all(
            item["snapshot_id"] == second["snapshot_id"] for item in matches
        )


def test_code_function_summaries_are_persisted_and_vectorized_incrementally(
    db_engine, tmp_path
):
    from app.adapters.local import LocalObjectStore
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.repositories.code_graph_repository import CodeGraphRepository
    from app.services.code_index_service import CodeIndexService
    from app.services.code_vector_service import CodeVectorService

    source = tmp_path / "function-summary-source"
    source.mkdir()
    timeout_file = source / "timeout.py"
    stable_file = source / "stable.py"
    timeout_file.write_text(
        "def handle_order_timeout(order_id):\n"
        "    cancel_order(order_id)\n"
        "    release_inventory(order_id)\n",
        encoding="utf-8",
    )
    stable_file.write_text("def health_check():\n    return True\n", encoding="utf-8")

    with SessionLocal() as db:
        index_service = CodeIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=LocalObjectStore(tmp_path / "function-summary-objects"),
            task_queue=_FakeTaskQueue(),
        )
        summary_llm = _FakeSummaryLLM()
        container = _container(summary_llm)
        vector_service = CodeVectorService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, container=container, summary_llm=summary_llm
        )

        index_service.index_directory(source, revision="summary-r1")
        first = vector_service.rebuild()
        first_generated_count = summary_llm.generated_symbol_count
        repository = CodeGraphRepository(db, DEFAULT_KNOWLEDGE_BASE_ID)
        timeout_symbol = next(
            item for item in repository.search_symbols("handle_order_timeout", 10)
            if item["kind"] == "FUNCTION"
        )
        vector_store = container.vector_store.indexes[first["index_name"]]
        function_vectors = [
            document for document in vector_store.documents
            if document.metadata.get("vector_kind") == "function_summary"
        ]

        assert timeout_symbol["functional_summary"] == (
            "处理订单超时，取消订单并释放库存。"
        )
        assert first["vector_count"] == first["symbol_count"] * 2
        assert len(function_vectors) == first["symbol_count"]
        assert any(
            "功能摘要：处理订单超时" in document.page_content
            for document in function_vectors
        )

        timeout_file.write_text(
            "def handle_order_timeout(order_id):\n"
            "    cancel_order(order_id)\n"
            "    release_inventory(order_id)\n"
            "    notify_customer(order_id)\n",
            encoding="utf-8",
        )
        index_service.index_directory(source, revision="summary-r2")
        second = vector_service.rebuild()

        assert second["index_name"] == first["index_name"]
        assert second["summaries_reused"] >= 2
        assert summary_llm.generated_symbol_count > first_generated_count
        assert second["vectors_reused"] >= 2
