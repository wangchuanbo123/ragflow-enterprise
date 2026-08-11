from pathlib import Path

from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.services.knowledge_graph_service import GraphBuildService
from rag.providers.interfaces import ProviderHealth, VectorIndexRef


ROOT = Path(__file__).resolve().parents[1]


class FakeGraphStore:
    def cleanup_orphans(self):
        pass

    def stats(self):
        return {"entities": 0, "relations": 0}


class FakeVectorProvider:
    def __init__(self):
        self.calls = []

    def health(self, index_ref, expected_ids=None):
        self.calls.append((index_ref, expected_ids))
        return ProviderHealth.ok(provider="fake_vector", vector_count=0)


def test_provider_health_serializes_without_vendor_objects():
    result = ProviderHealth.ok(provider="fake", vector_count=3).as_dict()

    assert result == {
        "provider": "fake",
        "vector_count": 3,
        "status": "ok",
    }


def test_vector_index_ref_is_application_owned():
    ref = VectorIndexRef(
        name="documents_v1",
        knowledge_base_id="kb-1",
        index_version="v1",
        embedding_profile_id="embedding-v1",
    )

    assert ref.name == "documents_v1"
    assert ref.knowledge_base_id == "kb-1"
    assert ref.embedding_profile_id == "embedding-v1"


def test_graph_build_service_accepts_injected_store(db_engine):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        store = FakeGraphStore()
        service = GraphBuildService(db, store=store)

        assert service.store is store


def test_readiness_accepts_injected_vector_provider(db_engine):
    from app.core.database import SessionLocal
    from app.repositories.knowledge_repository import KnowledgeRepository
    from app.services.readiness_service import _check_vector_store

    with SessionLocal() as db:
        KnowledgeRepository(db).set_active_collection("documents_v1")

    provider = FakeVectorProvider()
    result = _check_vector_store(provider)

    assert result["status"] == "ok"
    assert result["provider"] == "fake_vector"
    assert provider.calls[0][0] == VectorIndexRef(
        name="documents_v1",
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
    )
    assert provider.calls[0][1] == set()


def test_high_level_modules_do_not_import_concrete_storage_clients():
    forbidden = {
        "app/services/readiness_service.py": ("chromadb.PersistentClient",),
        "app/services/knowledge_graph_service.py": ("SqliteGraphStore",),
        "app/api/v1/knowledge_graph.py": ("SqliteGraphStore",),
        "rag/knowledge_graph/retriever.py": ("SqliteGraphStore",),
        "rag/runtime/runtime.py": ("VECTOR_DB_DIR", "chromadb"),
        "rag/indexing/index_manager.py": ("VECTOR_DB_DIR", "chromadb"),
        "app/services/document_index_service.py": ("DOC_DIR",),
        "app/services/index_worker.py": ("SessionLocal", "DOC_DIR / item.source_path"),
    }

    for relative_path, tokens in forbidden.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        for token in tokens:
            assert token not in source, f"{relative_path} imports {token}"
