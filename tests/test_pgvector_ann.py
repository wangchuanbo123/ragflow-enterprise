"""Real HNSW build/recall and exact filtered fallback in a disposable schema."""

import numpy as np
import pytest
from sqlalchemy import text

from tests.test_pgvector_provider import pgvector_engine
from rag.providers.interfaces import VectorIndexRef
from rag.providers.pgvector_store import PgVectorStoreProvider


def test_hnsw_mixed_dimensions_and_filtered_exact_recall(pgvector_engine):
    provider = PgVectorStoreProvider(pgvector_engine, search_mode="auto", ann_min_rows=100, ann_ef_search=200)
    ref = VectorIndexRef("ann-v1", "ann-kb")
    store = provider.open(None, ref)
    vectors = np.random.default_rng(61).normal(size=(1200, 32)).astype("float32")
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    store.add_embeddings([(str(index), vector) for index, vector in enumerate(vectors)],
        ids=[f"id-{index:04}" for index in range(len(vectors))],
        metadatas=[{"position": index, "document_id": f"doc-{index % 100}"} for index in range(len(vectors))])
    other = provider.open(None, VectorIndexRef("other-dimension", "other-kb"))
    other.add_embeddings([("must not leak", [1, 0, 0])], ids=["secret"])
    assert provider.maintain_search_index(ref)["status"] == "planned"
    result = provider.maintain_search_index(ref, apply=True)
    assert result["status"] == "ready" and result["dimension"] == 32
    assert provider.maintain_search_index(ref, apply=True)["status"] == "ready"
    with pgvector_engine.connect() as db:
        collection, _ = provider._rows_scope(db, ref)
        assert not provider._ann_available(db, collection)
    quality = provider.validate_search_index(ref, apply=True, min_recall=.9)
    assert quality["index_used"], "validation must execute the HNSW index"
    assert quality["min_recall"] >= .9, quality["min_recall"]
    assert quality["p95_ms"] <= 1000, quality["p95_ms"]
    assert quality["passed"] and quality["accepted"]
    collection = None
    with pgvector_engine.connect() as db:
        collection, _ = provider._rows_scope(db, ref)
        assert provider._ann_available(db, collection)
        db.execute(text("SET LOCAL enable_seqscan=off"))
        query_vector = "[" + ",".join(map(str, vectors[0])) + "]"
        explain = db.execute(text("EXPLAIN SELECT id FROM pgvector_embeddings WHERE collection_id=:id "
            "ORDER BY embedding::vector(32) <=> CAST(:vector AS vector(32)) LIMIT 10"),
            {"id": collection["id"], "vector": query_vector}).scalars().all()
        assert result["index"] in " ".join(explain)
    for index in (0, 11, 97):
        expected = set(np.argsort(-(vectors @ vectors[index]))[:10])
        rows = store.similarity_search_by_vector(vectors[index], k=10)
        actual = {row.metadata["position"] for row in rows}
        assert len(actual) == 10 and len(expected & actual) / 10 >= .9
        selected = np.array([position for position in range(len(vectors)) if position % 100 == 3])
        expected_filtered = set(selected[np.argsort(-(vectors[selected] @ vectors[index]))[:10]])
        filtered = store.similarity_search_by_vector(vectors[index], k=10, filter={"document_id": "doc-3"})
        assert {row.metadata["position"] for row in filtered} == expected_filtered
    with pytest.raises(ValueError, match="another|not found|does not exist|Unknown|not configured"):
        provider.maintain_search_index(VectorIndexRef("ann-v1", "other-kb"), apply=True)
    with pgvector_engine.begin() as db:
        db.execute(text(f'DROP INDEX "{result["index"]}"'))
        assert not provider._ann_available(db, collection)
    assert len(store.similarity_search_by_vector(vectors[0], k=10)) == 10


def test_hnsw_limits_are_explicit_and_exact_mode_is_available(pgvector_engine):
    provider = PgVectorStoreProvider(pgvector_engine, search_mode="exact", ann_min_rows=2)
    ref = VectorIndexRef("small-v1", "small-kb")
    store = provider.open(None, ref)
    store.add_embeddings([("a", [1, 0]), ("b", [0, 1])], ids=["a", "b"])
    with pytest.raises(ValueError):
        provider.maintain_search_index(ref, min_rows=0)
    assert provider.maintain_search_index(ref, apply=True, min_rows=1)["status"] == "ready"
    with pgvector_engine.connect() as db:
        collection, _ = provider._rows_scope(db, ref)
        assert not provider._ann_available(db, collection)
    assert store.similarity_search_by_vector([1, 0], k=1)[0].page_content == "a"
