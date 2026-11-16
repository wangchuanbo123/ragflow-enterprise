"""Cache correctness under real writes, permission changes and scoped queries."""

import copy
import time

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_distributed_coordination import postgres_service
from tests.test_graph_filters import seed, Backend, plan, condition
from app.models.knowledge_graph import KgEntity
from app.models.knowledge_base import KnowledgeBaseMember
from app.schemas.graph_filter import GraphFilterPreviewRequest, GraphFilterPlan
from app.services.graph_filter_service import GraphFilterService, GraphFilterError
from app.services.graph_filter_query import load_snapshot, execute_plan
from app.services.graph_revision import current_revision


@pytest.mark.parametrize("database", ["db_engine", "postgres_service"])
def test_revision_observes_bulk_sql_and_rolls_back_atomically(request, database):
    value = request.getfixturevalue(database)
    engine = value[0] if database == "postgres_service" else value
    with Session(engine) as db:
        seed(db)
        original = current_revision(db, "filter-kb")
        private = current_revision(db, "private-kb")
        db.execute(text("UPDATE kg_entities SET description='new' WHERE id='a'"))
        changed = current_revision(db, "filter-kb")
        assert changed[0] == original[0] and changed[1] > original[1]
        assert current_revision(db, "private-kb") == private
        db.rollback()
        assert current_revision(db, "filter-kb") == original
        db.execute(text("UPDATE knowledge_chunks SET content='new raw text' WHERE id='chunk-doc-a'"))
        db.commit()
        assert current_revision(db, "filter-kb")[1] > original[1]


def test_postgres_statement_trigger_bumps_once_and_truncate_invalidates(postgres_service):
    engine, _, _ = postgres_service
    with Session(engine) as db:
        seed(db, extra=40)
        before = current_revision(db, "filter-kb")
        db.execute(text("UPDATE kg_entities SET description='bulk changed' WHERE knowledge_base_id='filter-kb'"))
        assert current_revision(db, "filter-kb")[1] == before[1] + 1
        db.commit()
        before = current_revision(db, "filter-kb")
        db.execute(text("TRUNCATE kg_aliases"))
        assert current_revision(db, "filter-kb")[1] > before[1]
        db.rollback()
        assert current_revision(db, "filter-kb") == before


def test_snapshot_cached_but_each_use_reauthorizes_and_invalidates(db_engine, monkeypatch):
    import app.services.graph_filter_service as module
    with Session(db_engine) as db:
        seed(db, extra=120)
    loads = []
    original = module.load_snapshot
    def record(*args, **kwargs):
        loads.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "load_snapshot", record)
    empty = GraphFilterPreviewRequest(principles=[])
    with Session(db_engine) as db:
        first = GraphFilterService(db, "filter-kb", "reader").preview(empty)
        second = GraphFilterService(db, "filter-kb", "reader").preview(empty)
        assert first.graph_fingerprint == second.graph_fingerprint and len(loads) == 1
        row = db.get(KgEntity, "b")
        row.description = "committed change"
        db.commit()
        third = GraphFilterService(db, "filter-kb", "reader").preview(empty)
        assert third.graph_fingerprint != first.graph_fingerprint and len(loads) == 2
        db.query(KnowledgeBaseMember).filter_by(knowledge_base_id="filter-kb", user_id="reader").delete()
        db.commit()
        with pytest.raises(GraphFilterError) as denied:
            GraphFilterService(db, "filter-kb", "reader").preview(empty)
        assert denied.value.status == 403


def test_compilation_cache_is_user_model_and_revision_scoped(db_engine, monkeypatch):
    with Session(db_engine) as db:
        seed(db)
    backend = Backend(plan([{"hide_isolated": True}]))
    backend.identity = lambda: {"fingerprint": "configured-model-v1"}
    monkeypatch.setattr("app.agents.factory.get_agent_backend", lambda **kwargs: backend)
    payload = GraphFilterPreviewRequest(principles=["隐藏孤点"])
    with Session(db_engine) as db:
        service = GraphFilterService(db, "filter-kb", "reader")
        service.preview(payload)
        service.preview(payload)
        assert len(backend.requests) == 1
        GraphFilterService(db, "filter-kb", "reader2").preview(payload)
        assert len(backend.requests) == 2
        backend.identity = lambda: {"fingerprint": "configured-model-v2"}
        service.preview(payload)
        assert len(backend.requests) == 3
        db.get(KgEntity, "b").description = "new fact version"
        db.commit()
        service.preview(payload)
        assert len(backend.requests) == 4


def test_indexed_prefilter_preserves_overlay_evidence_and_or_semantics(db_engine):
    with Session(db_engine) as db:
        seed(db, extra=240)
        snapshot = load_snapshot(db, "filter-kb", time.monotonic() + 30)
    plans = [plan([{"conditions": [condition("edge", "predicate", values=["USES"])]}]),
             plan([{"conditions": [condition("edge", "has_evidence", "eq", boolean=True)]}]),
             plan([{"conditions": [condition("node", "entity_type", values=["system"])]},
                   {"conditions": [condition("node", "name", "contains_any", values=["普通"])]}]),
             plan([{"conditions": [condition("edge", "source_document", values=["doc-b"])]}])]
    for value in plans:
        current = GraphFilterPlan.model_validate(value)
        indexed = execute_plan(snapshot, current, time.monotonic() + 30)
        scan = copy.copy(snapshot)
        scan.query_indexes = {}
        assert execute_plan(scan, current, time.monotonic() + 30) == indexed


def test_token_cache_reuses_unchanged_content_and_never_pickle(db_engine, tmp_path, monkeypatch):
    from app.core import config
    import rag.retrievers.bm25_retriever as module
    from app.models.knowledge_chunk import KnowledgeChunk
    monkeypatch.setattr(config, "BM25_TOKEN_CACHE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(config, "BM25_TOKEN_CACHE_ENABLED", True, raising=False)
    with Session(db_engine) as db:
        seed(db)
        first = module.ChunkBM25Retriever("filter-kb")
        first.build(db)
        assert first.build_stats["tokenized_chunks"] == 2
        second = module.ChunkBM25Retriever("filter-kb")
        second.build(db)
        assert second.build_stats["reused_tokens"] == 2
        db.get(KnowledgeChunk, "chunk-doc-a").content += "新的词语"
        db.commit()
        second.build(db)
        assert second.build_stats["reused_tokens"] == 1 and second.build_stats["tokenized_chunks"] == 1
        assert first.get_relevant_documents("生产系统", allowed_document_ids={"doc-b"})[0].metadata["document_id"] == "doc-b"
    assert list(tmp_path.glob("*.json.gz")) and not list(tmp_path.glob("*.pkl"))


def test_bounded_cache_does_not_keep_oversized_snapshots():
    from app.services.graph_filter_cache import VersionedCache
    cache = VersionedCache(max_entries=2, max_weight=3)
    cache.put("a", "one", weight=2)
    cache.put("b", "two", weight=2)
    assert cache.get("a") is None and cache.get("b") == "two"
    cache.put("large", "large", weight=4)
    assert cache.get("large") is None


def test_cumulative_counts_identify_rule_that_empties_graph(db_engine):
    with Session(db_engine) as db:
        seed(db, extra=30)
        backend = Backend(plan([{"conditions": [condition("node", "entity_type", values=["system"])]}],
                               [{"conditions": [condition("node", "entity_type", values=["person"])]}]))
        response = GraphFilterService(db, "filter-kb", "reader", backend=backend).preview(
            GraphFilterPreviewRequest(principles=["只看系统", "只看人物"]))
        assert response.per_principle_counts[0].matched_nodes == 6
        assert response.per_principle_counts[1].matched_nodes == 0
        assert response.stats.matched_nodes == 0


def test_ambiguity_candidates_are_current_knowledge_base_only(db_engine):
    from app.models.knowledge_document import KnowledgeDocument
    with Session(db_engine) as db:
        seed(db)
        db.get(KnowledgeDocument, "doc-b").original_filename = "运行手册.txt"
        db.commit()
        backend = Backend(plan([{"conditions": [condition("edge", "source_document", values=["运行手册.txt"])]}]))
        with pytest.raises(GraphFilterError) as failed:
            GraphFilterService(db, "filter-kb", "reader", backend=backend).preview(
                GraphFilterPreviewRequest(principles=["只看运行手册的关系"]))
        candidates = failed.value.details["unapplied_principles"][0]["candidates"]
        assert {value["value"] for value in candidates} == {"doc-a", "doc-b"}
        assert "secret" not in str(candidates)
