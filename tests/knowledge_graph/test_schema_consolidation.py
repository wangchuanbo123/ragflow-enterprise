"""Lossless legacy migration, API retirement and paired endpoint constraints."""
from copy import deepcopy
from datetime import datetime
import json
import os
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.orm import Session

from app.core.database import create_database_engine, normalize_database_url
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
from app.services.schema_consolidation import normalize_schema_rows


def legacy_rows():
    result = []
    for version, kind in enumerate(["general", "concept", "functional", "configuration", "standard"], 1):
        source, target = ("A", "B") if version % 2 else ("C", "D")
        result.append(dict(id="schema-" + kind, knowledge_base_id=KB, graph_kind=kind, version=version,
            status="active", entity_types_json=[source, target], relation_types_json=["LINKS"],
            normalization_rules_json={"case": kind}, graph_profiles_json=None,
            generation_summary_json={"entity_type_details": [{"name": source, "description": kind + " definition"}],
                "relation_type_details": [{"name": "LINKS", "description": kind + " relation", "source_types": [source], "target_types": [target], "direction": "directed"}]},
            extraction_prompt=kind + " extraction rules", generation_method="manual", created_by=None,
            source_document_ids_json=[], created_at=datetime(2026, 1, version), activated_at=datetime(2026, 1, version)))
    return result


def test_lossless_pure_transform_preserves_definitions_rules_and_original_rows():
    rows = legacy_rows()
    original = deepcopy(rows)
    normalized = normalize_schema_rows(rows)
    assert rows == original
    assert len(normalized) == 6
    merged = next(row for row in normalized if row["status"] == "active")
    assert merged["graph_kind"] == "general" and merged["version"] == 6
    assert merged["entity_types_json"] == ["A", "B", "C", "D"]
    assert all(row["status"] == "superseded" for row in normalized[:5])
    summary = merged["generation_summary_json"]
    assert len(summary["legacy_schemas"]) == 5
    assert len(summary["normalization_rule_sources"]) == 5
    for row in original:
        assert row["extraction_prompt"] in merged["extraction_prompt"]
    assert len(summary["relation_type_details"][0]["endpoint_variants"]) == 2
    assert normalize_schema_rows(normalized) == normalized
    exported = json.loads(json.dumps(rows, default=lambda value: value.isoformat()))
    assert normalize_schema_rows(exported)[-1]["created_at"].endswith("Z") is False


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_h4_upgrade_preserves_history_facts_and_normalizes_unfinished_work(tmp_path, dialect, monkeypatch):
    root = None
    if dialect == "postgresql":
        if not os.getenv("TEST_POSTGRES_URL"):
            pytest.skip("TEST_POSTGRES_URL is required for isolated real PostgreSQL migrations")
        url = normalize_database_url(os.environ["TEST_POSTGRES_URL"])
        root = create_database_engine(url)
        name = "test_consolidation_" + uuid.uuid4().hex
        with root.begin() as connection:
            connection.execute(text("CREATE SCHEMA " + name))
        engine = create_database_engine(url.set(query={**url.query, "options": "-csearch_path=" + name + ",public"}))
    else:
        engine = create_database_engine("sqlite:///" + (tmp_path / "migration.sqlite").as_posix())
    config = Config("alembic.ini")
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "h4f6a8b0c213")
            metadata = MetaData()
            schemas = Table("graph_schema_definitions", metadata, autoload_with=connection)
            jobs = Table("index_jobs", metadata, autoload_with=connection)
            connection.execute(schemas.insert(), legacy_rows())
            connection.execute(jobs.insert().values(id="legacy-pending", knowledge_base_id=KB, job_type="schema_generation",
                status="pending", total_items=1, processed_items=0, failed_items=0, succeeded_items=0, attempt_count=0, max_attempts=3, cancel_requested=False,
                options_json={"graph_kind": "functional", "target_schema_id": "schema-functional"}))
            connection.commit()
            from tests.test_graph_schema_induction_and_coverage import _seed_document
            from app.models.knowledge_chunk import KnowledgeChunk
            from app.models.knowledge_graph import KgEntity, KgEntityMention
            with Session(connection) as db:
                document, chunks = _seed_document(db)
                chunks[0].graph_schema_version = 1
                chunks[1].graph_schema_version = 0
                chunks[2].graph_schema_version = 5
                entity = KgEntity(knowledge_base_id=KB, entity_type="A", canonical_name="保留实体", normalized_name="保留实体")
                db.add(entity)
                db.flush()
                db.add(KgEntityMention(knowledge_base_id=KB, entity_id=entity.id, document_id=document.id, chunk_id=chunks[0].id, mention="保留证据"))
                db.commit()
                entity_id = entity.id
            connection.commit()
            command.upgrade(config, "head")
            rows = list(connection.execute(select(schemas)).mappings())
            assert len(rows) == 6
            assert [(row["graph_kind"], row["version"]) for row in rows if row["status"] == "active"] == [("general", 6)]
            options = connection.scalar(select(jobs.c.options_json).where(jobs.c.id == "legacy-pending"))
            assert options["graph_kind"] == "general" and options["legacy_graph_kind"] == "functional"
            assert "target_schema_id" not in options and options["legacy_target_schema_id"] == "schema-functional"
            with Session(connection) as db:
                from app.services.knowledge_graph_service import GraphBuildService
                from types import SimpleNamespace
                monkeypatch.setattr("app.bootstrap.container.get_provider_container", lambda: SimpleNamespace(task_queue=object()))
                assert GraphBuildService(db, store=object(), knowledge_base_id=KB)._get_chunks_to_process(True, False) == []
                assert db.get(KgEntity, entity_id).canonical_name == "保留实体"
                assert db.scalar(select(KgEntityMention.mention)) == "保留证据"
                assert list(db.scalars(select(KnowledgeChunk.graph_schema_version).order_by(KnowledgeChunk.chunk_index))) == [1, 0, 5]
            command.check(config)
    finally:
        engine.dispose()
        if root:
            with root.begin() as connection:
                connection.execute(text("DROP SCHEMA " + name + " CASCADE"))
            root.dispose()


def test_paired_endpoint_alternatives_do_not_authorize_cartesian_cross_pairs():
    from rag.knowledge_graph.extractor import GraphExtractor, parse_extraction_response
    extractor = GraphExtractor(llm=object())
    extractor.configure_schema(entity_types=["A", "B", "C", "D"], relation_types=["LINKS"],
        relation_constraints=[{"name": "LINKS", "endpoint_variants": [
            {"source_types": ["A"], "target_types": ["B"]}, {"source_types": ["C"], "target_types": ["D"]}]}])
    for source, target, accepted in [("A", "B", True), ("C", "D", True), ("A", "D", False), ("C", "B", False)]:
        output = {"entities": [{"local_id": "s", "name": "甲", "type": source}, {"local_id": "t", "name": "乙", "type": target}],
                  "relations": [{"subject": "s", "object": "t", "predicate": "LINKS", "evidence": "甲联系乙", "confidence": .9}]}
        result = parse_extraction_response(json.dumps(output), "chunk", "甲联系乙", extractor.entity_types,
            extractor.relation_types, extractor.relation_constraints, extractor.relation_constraint_variants)
        assert bool(result.relations) == accepted


def test_agent_extraction_enforces_the_same_paired_constraints_and_removed_types():
    from rag.knowledge_graph.agent_extractor import AgentGraphExtractor
    extractor = AgentGraphExtractor("kb", backend=object())
    extractor.configure_schema(entity_types=["A", "B", "C", "D"], relation_types=["LINKS"],
        relation_constraints=[{"name": "LINKS", "endpoint_variants": [
            {"source_types": ["A"], "target_types": ["B"]}, {"source_types": ["C"], "target_types": ["D"]}]}])
    chunk = {"chunk_id": "c", "content": "甲联系乙"}
    relation = {"subject": "s", "object": "t", "predicate": "LINKS", "evidence": "甲联系乙", "confidence": .9}
    def output(source, target):
        return {"chunks": [{"chunk_id": "c", "entities": [{"local_id": "s", "name": "甲", "type": source},
            {"local_id": "t", "name": "乙", "type": target}], "relations": [relation],
            "review": {"context_resolved": True, "coverage_checked": True, "conflicts_checked": True, "issues": []}}]}
    assert extractor._validate(output("A", "B"), [chunk])[0].relations
    with pytest.raises(ValueError, match="端点类型"):
        extractor._validate(output("A", "D"), [chunk])
    extractor.configure_schema(entity_types=["A", "D"], relation_types=["LINKS"],
        relation_constraints=[{"name": "LINKS", "source_types": ["REMOVED"], "target_types": ["D"]}])
    with pytest.raises(ValueError, match="端点类型"):
        extractor._validate(output("A", "D"), [chunk])
