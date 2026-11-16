"""Provider security/contract tests; real PostgreSQL uses a disposable schema."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import uuid

from langchain_core.documents import Document
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateSchema, DropSchema

from rag.providers.interfaces import VectorIndexRef
from rag.providers.pgvector_store import PGVECTOR_METADATA, PgVectorStoreProvider, metadata_filter, _vector


class Embedding:
    def embed_documents(self, texts):
        return [self.embed_query(value) for value in texts]

    def embed_query(self, text):
        return {"east": [1.0, 0, 0], "north": [0, 1.0, 0], "west": [-1.0, 0, 0]}.get(text, [1.0, 1.0, 0])


@pytest.fixture
def pgvector_engine():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to run isolated pgvector integration tests")
    schema = "test_pgvector_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        connection.execute(CreateSchema(schema))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        PGVECTOR_METADATA.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.mark.parametrize("where", [{}, {"$not": {}}, {"$or": []}, {"$and": [{"a": 1}]},
    {"a": {"$regex": "secret"}}, {"a": {"$in": "secret"}}, {"a": {"$gt": "1"}},
    {"a": {"$gte": True}}, {"a": float("nan")}, {"a": {}}, {"a": {"$eq": []}}])
def test_invalid_filters_never_fall_back_to_unfiltered_search(where):
    with pytest.raises(ValueError):
        metadata_filter(where)


def test_metadata_values_and_field_names_are_bound_not_sql_fragments():
    key, value = "x'); DROP TABLE anything; --", "secret' OR TRUE --"
    compiled = metadata_filter({key: value}).compile(dialect=postgresql.dialect())
    assert key not in str(compiled) and value not in str(compiled)
    assert key in json.dumps(compiled.params) and value in json.dumps(compiled.params)


@pytest.mark.parametrize("vector", [[], [0, 0], [True, 1], [float("nan"), 1], [float("inf")], [1e100], [1e-50, 0], "123"])
def test_invalid_cosine_vectors_are_rejected(vector):
    with pytest.raises(ValueError):
        _vector(vector)


def test_populated_legacy_index_is_never_bound_to_current_model_silently(pgvector_engine):
    from rag.providers.pgvector_store import COLLECTIONS
    from sqlalchemy import select
    provider = PgVectorStoreProvider(pgvector_engine)
    legacy = VectorIndexRef("legacy-unverified", "legacy-kb")
    provider.open(Embedding(), legacy).add_texts(["east"], ids=["legacy-row"])
    requested = VectorIndexRef(legacy.name, legacy.knowledge_base_id, "parser-v2", "same-dimension-new-model")
    with pytest.raises(ValueError, match="Legacy vector identity is unverified"):
        provider.open(Embedding(), requested)
    with pgvector_engine.connect() as db:
        row = db.execute(select(COLLECTIONS).where(COLLECTIONS.c.name == legacy.name)).mappings().one()
        assert row["embedding_profile_id"] is None and row["index_version"] is None
    assert provider.count(legacy) == 1


@pytest.mark.parametrize("kind", ["documents", "entities", "relations", "code"])
def test_real_pgvector_scopes_versions_filters_relevance_and_exports(pgvector_engine, kind):
    provider = PgVectorStoreProvider(pgvector_engine, batch_size=1)
    ref = VectorIndexRef(f"{kind}_v1", "kb-a", "v1", "model-a")
    other_ref = VectorIndexRef(ref.name, "kb-b")
    store = provider.open(Embedding(), ref)
    other = provider.open(Embedding(), other_ref)
    ids = [f"{kind}:east", f"{kind}:north", f"{kind}:west"]
    store.add_documents([Document(page_content=name, metadata={"document_id": f"doc-{i}", "rank": i, "flag": i == 1})
        for i, name in enumerate(("east", "north", "west"))], ids=ids)
    other.add_texts(["secret"], ids=[ids[0]], metadatas=[{"document_id": "forbidden"}])
    assert provider.health(ref, set(ids)).status == "ok"
    assert provider.health(ref, {"wrong"}).status == "fail"
    assert provider.count(ref) == 3
    assert provider.list_ids(other_ref) == {ids[0]}
    assert len(provider.list_indexes(knowledge_base_id="kb-a")) == 1
    rows = store.similarity_search_with_score("east", k=3)
    assert [doc.page_content for doc, _ in rows] == ["east", "north", "west"]
    assert [score for _, score in rows] == pytest.approx([0, 1, 2])
    rows = store.similarity_search_by_vector_with_relevance_scores([1, 0, 0], k=3)
    assert [score for _, score in rows] == pytest.approx([1, 0.5, 0])
    assert [doc.page_content for doc, _ in store.similarity_search_with_relevance_scores("east", score_threshold=0.6)] == ["east"]
    assert store.similarity_search("east", filter={"document_id": {"$in": []}}) == []
    assert [doc.page_content for doc in store.similarity_search("east", filter={"$and": [{"rank": {"$gte": 1}}, {"rank": {"$lt": 2}}]})] == ["north"]
    assert provider.list_ids(ref, {"flag": True}) == {ids[1]}
    retriever = store.as_retriever(search_kwargs={"k": 1, "filter": {"document_id": "doc-1"}})
    assert [doc.page_content for doc in retriever.invoke("east")] == ["north"]
    assert provider.list_ids(ref, {"rank": {"$ne": 0}}) == {ids[1], ids[2]}
    assert provider.list_ids(ref, {"rank": {"$nin": [0, 2]}}) == {ids[1]}
    assert provider.list_ids(ref, {"missing": {"$ne": "x"}}) == set()
    store.add_texts(["north"], ids=[ids[0]], metadatas=[{"document_id": "doc-0", "rank": "not a number"}])
    assert provider.count(ref) == 3
    assert provider.list_ids(ref, {"rank": {"$gte": 1}}) == set(ids[1:])
    exported = store.get(ids=[ids[0]], include=["documents", "metadatas", "embeddings"])
    assert exported["documents"] == ["north"] and exported["embeddings"] == [[0, 1, 0]]
    with pytest.raises(ValueError, match="another knowledge base"):
        store.add_texts(["east"], ids=["forged"], metadatas=[{"knowledge_base_id": "kb-b"}])
    with pytest.raises(ValueError, match="dimension"):
        store.add_embeddings([("new", [1, 0])], ids=["mismatched"])
    with pytest.raises(ValueError, match="profile"):
        provider.open(Embedding(), VectorIndexRef(ref.name, "kb-a", embedding_profile_id="wrong"))
    v2 = provider.open(Embedding(), VectorIndexRef(f"{kind}_v2", "kb-a", "v2"))
    v2.add_embeddings([("new model", [1, 0])], ids=["new"])
    provider.delete_vectors(ref, ids=ids[:2], where={"document_id": "doc-1"})
    assert provider.list_ids(ref) == {ids[0], ids[2]}
    with pytest.raises(ValueError):
        provider.delete_vectors(ref, where={})
    provider.delete_index(ref)
    assert provider.count(other_ref) == 1 and provider.count(v2.index_ref) == 1
    assert provider.health(ref).status == "fail"


def test_batch_failure_rolls_back_all_rows_and_dimension(pgvector_engine):
    provider = PgVectorStoreProvider(pgvector_engine, batch_size=1)
    ref = VectorIndexRef("atomic", "kb-a")
    store = provider.open(Embedding(), ref)
    writes = []
    def reject_second(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO pgvector_embeddings"):
            writes.append(True)
            if len(writes) == 2:
                raise RuntimeError("synthetic transaction failure")
    event.listen(pgvector_engine, "before_cursor_execute", reject_second)
    try:
        with pytest.raises(RuntimeError):
            store.add_texts(["east", "north"], ids=["one", "two"])
    finally:
        event.remove(pgvector_engine, "before_cursor_execute", reject_second)
    assert provider.count(ref) == 0
    assert provider.health(ref).metadata["dimension"] is None


def test_factory_reuses_the_application_postgresql_engine(pgvector_engine, monkeypatch):
    from app.core import database
    from rag.providers import factory
    monkeypatch.setattr(factory, "VECTOR_STORE_PROVIDER", "pgvector")
    monkeypatch.setattr(database, "engine", pgvector_engine)
    factory.get_vector_store_provider.cache_clear()
    try:
        provider = factory.get_vector_store_provider()
        assert isinstance(provider, PgVectorStoreProvider) and provider.engine is pgvector_engine
        assert provider.list_indexes() == []
    finally:
        factory.get_vector_store_provider.cache_clear()


def test_concurrent_first_writes_cannot_mix_dimensions(pgvector_engine):
    provider = PgVectorStoreProvider(pgvector_engine)
    store = provider.open(Embedding(), VectorIndexRef("dimension-race", "kb-a"))
    def write(vector):
        try:
            store.add_embeddings([("sample", vector)], ids=[str(len(vector))])
            return "ok"
        except ValueError:
            return "dimension mismatch"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, ([1, 0], [1, 0, 0]))) == ["dimension mismatch", "ok"]
    assert provider.count(store.index_ref) == 1


def test_provider_does_not_issue_request_time_ddl_or_use_unscoped_default(pgvector_engine):
    provider = PgVectorStoreProvider(pgvector_engine)
    statements = []
    def record(connection, cursor, statement, *args):
        statements.append(statement)
    event.listen(pgvector_engine, "before_cursor_execute", record)
    try:
        store = provider.open(Embedding(), VectorIndexRef(None, "kb-a"))
        assert store.similarity_search("east") == []
        assert provider.list_indexes() == []
        with pytest.raises(ValueError):
            store.add_texts(["east"])
        with pytest.raises(ValueError):
            provider.open(Embedding(), VectorIndexRef("missing-kb"))
        provider.open(Embedding(), VectorIndexRef("declared", "kb-a"))
    finally:
        event.remove(pgvector_engine, "before_cursor_execute", record)
    assert not any(statement.lstrip().upper().startswith(("CREATE", "ALTER", "DROP")) for statement in statements)


def test_real_application_document_entity_relation_and_code_services_use_pgvector(pgvector_engine, db_engine, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
    from app.adapters.local import LocalObjectStore
    from app.services.entity_vector_service import EntityVectorService
    from app.services.relation_vector_service import RelationVectorService
    from app.services.code_index_service import CodeIndexService
    from app.services.code_vector_service import CodeVectorService
    from tests.services.test_agent_workflows import seed
    from tests.test_retrieval_consistency import DeterministicEmbedding, index_text
    from tests.test_relation_and_code_vector_retrieval import _FakeSummaryLLM
    provider = PgVectorStoreProvider(pgvector_engine)
    container = SimpleNamespace(vector_store=provider, embedding=DeterministicEmbedding(),
        object_store=LocalObjectStore(tmp_path / "objects"), task_queue=SimpleNamespace(enqueue=lambda identifier: None))
    monkeypatch.setattr("app.bootstrap.container.get_provider_container", lambda: container)
    with SessionLocal() as db:
        seed(db)
        index_text(db, container, "a.txt", "original document with service dependencies")
        assert provider.count(VectorIndexRef("audit_active", KB)) > 0
        entities = EntityVectorService(db, KB, container)
        assert entities.rebuild()["status"] == "ready"
        assert entities.semantic_search("系统", query_embedding=[1, 0])
        relations = RelationVectorService(db, KB, container)
        assert relations.rebuild()["status"] == "ready"
        assert relations.semantic_search("系统依赖", query_embedding=[1, 0])
        source = tmp_path / "code"
        source.mkdir()
        (source / "calculate.py").write_text("def calculate(value):\n    return value + 1\n", encoding="utf-8")
        CodeIndexService(db, KB, container.object_store, container.task_queue).index_directory(source)
        code = CodeVectorService(db, KB, container, summary_llm=_FakeSummaryLLM())
        assert code.rebuild()["status"] == "ready"
        assert code.semantic_search("calculate", query_embedding=[1, 0])
        assert len(provider.list_indexes(knowledge_base_id=KB)) == 4
        old_names = [service.state().index_name for service in (entities, relations, code)]
        class ChangedEmbedding(DeterministicEmbedding):
            def identity(self):
                from rag.providers.interfaces import ProviderIdentity
                return ProviderIdentity(kind="embedding", provider="test", model="different-dimension")
            def embed_query(self, text):
                return [1, 0, 1]
        container.embedding = ChangedEmbedding()
        for service, old_name in zip((entities, relations, code), old_names):
            assert service.rebuild()["status"] == "ready"
            assert service.state().index_name != old_name
            assert provider.health(VectorIndexRef(service.state().index_name, KB)).metadata["dimension"] == 3
            assert service.semantic_search("query", query_embedding=[1, 0, 1])
