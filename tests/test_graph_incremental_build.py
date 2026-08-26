from __future__ import annotations

from types import SimpleNamespace

from langchain_core.documents import Document


class _FakeEmbedding:
    def identity(self):
        from rag.providers.interfaces import ProviderIdentity

        return ProviderIdentity(
            kind="embedding", provider="fake", model="fake-embedding"
        )

    def get_model(self):
        return self


class _FakeVectorStore:
    def __init__(self):
        self.documents: list[Document] = []
        self.ids: list[str] = []

    def add_documents(self, documents, ids=None):
        for document, vector_id in zip(documents, ids or []):
            if vector_id in self.ids:
                position = self.ids.index(vector_id)
                self.documents[position] = document
            else:
                self.ids.append(vector_id)
                self.documents.append(document)

    def delete(self, ids):
        removed = set(ids)
        kept = [
            (vector_id, document)
            for vector_id, document in zip(self.ids, self.documents)
            if vector_id not in removed
        ]
        self.ids = [item[0] for item in kept]
        self.documents = [item[1] for item in kept]


class _FakeVectorProvider:
    def __init__(self):
        self.indexes: dict[str, _FakeVectorStore] = {}

    def open(self, embedding, index_ref):
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


def _container():
    return SimpleNamespace(
        embedding=_FakeEmbedding(),
        vector_store=_FakeVectorProvider(),
        task_queue=_FakeTaskQueue(),
    )


def _make_document(db, *, path="graph-incremental.txt"):
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_document import KnowledgeDocument

    document = KnowledgeDocument(
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        source_path=path,
        original_filename=path,
        file_hash=f"hash-{path}",
        file_size=10,
        status="ready",
    )
    db.add(document)
    db.flush()
    return document


def _make_chunk(db, document, index, content, *, chunk_id=None, graph_status="pending"):
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_chunk import KnowledgeChunk

    chunk = KnowledgeChunk(
        id=chunk_id or f"{document.source_path}-chunk-{index}",
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        document_id=document.id,
        file_hash=document.file_hash,
        chunk_index=index,
        content=content,
        content_hash=f"content-hash-{document.source_path}-{index}",
        token_count=len(content),
        graph_status=graph_status,
    )
    db.add(chunk)
    db.flush()
    return chunk


def _upsert_fact(db, chunk, subject, obj, evidence, predicate="USES", chunk_id=None):
    from rag.knowledge_graph.schemas import (
        ChunkExtraction,
        ExtractedEntity,
        ExtractedRelation,
    )
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID

    store = SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID)
    store.upsert_extraction(
        {"document_id": chunk.document_id, "chunk_id": chunk_id or chunk.id},
        ChunkExtraction(
            chunk_id=chunk_id or chunk.id,
            entities=[
                ExtractedEntity(local_id="a", name=subject, type="SYSTEM"),
                ExtractedEntity(local_id="b", name=obj, type="SYSTEM"),
            ],
            relations=[
                ExtractedRelation(
                    subject="a",
                    predicate=predicate,
                    object="b",
                    evidence=evidence,
                    confidence=0.9,
                )
            ],
        ),
    )


# ---------------------------------------------------------------------------
# 1. Chunk 预过滤
# ---------------------------------------------------------------------------


def test_prefilter_detects_noise_and_signal():
    from rag.knowledge_graph.prefilter import has_graph_signal

    noise_samples = [
        "",
        "1. 2. 3. 4.",
        ".......................... 12",
        "版权所有 © 2026 示例公司",
        "第 3 页 / 共 120 页",
        "机密文件",
        "| 列一 | 列二 | 列三 |",
        "- - - -",
        "2023.05.12",
    ]
    for sample in noise_samples:
        assert not has_graph_signal(sample), sample

    signal_samples = [
        "支付服务使用风控服务完成交易校验，并在失败时通知订单中心。",
        "The payment service depends on the risk-control service, and it "
        "notifies the order center when validation fails.",
        "任务调度模块负责编排 nightly ETL 任务，它依赖消息队列组件传递控制信号。",
    ]
    for sample in signal_samples:
        assert has_graph_signal(sample), sample


def test_build_graph_prefilters_noise_without_llm(db_engine):
    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.services.knowledge_graph_service import GraphBuildService
    from rag.knowledge_graph.schemas import ChunkExtraction

    class CountingExtractor:
        def __init__(self):
            self.calls = 0
            self.seen_chunks = []

        def configure_schema(self, **kwargs):
            pass

        def extract_batch(self, chunks, llm=None):
            self.calls += 1
            self.seen_chunks.extend(item["chunk_id"] for item in chunks)
            return [
                ChunkExtraction(chunk_id=item["chunk_id"], entities=[], relations=[])
                for item in chunks
            ]

    with SessionLocal() as db:
        document = _make_document(db, path="prefilter.txt")
        _make_chunk(db, document, 0, "目录\n1. 概述 ................ 3\n2. 架构 .............. 17\n3. 部署 .............. 42")
        _make_chunk(db, document, 1, "版权所有 © 2026 示例公司，保留所有权利。")
        _make_chunk(db, document, 2, "支付服务使用风控服务完成交易校验，并在失败时通知订单中心。")
        db.commit()

        extractor = CountingExtractor()
        result = GraphBuildService(db, extractor=extractor).build_graph()

        assert result["prefiltered_chunks"] == 2
        assert result["total_chunks"] == 3
        assert extractor.calls == 1
        assert extractor.seen_chunks == ["prefilter.txt-chunk-2"]
        assert result["succeeded"] == 1
        assert {
            chunk.graph_status for chunk in db.query(KnowledgeChunk).all()
        } == {"completed"}


# ---------------------------------------------------------------------------
# 2. 变化集
# ---------------------------------------------------------------------------


def test_build_graph_returns_changeset(db_engine):
    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.services.knowledge_graph_service import GraphBuildService
    from rag.knowledge_graph.schemas import ChunkExtraction, ExtractedEntity, ExtractedRelation

    class TwoPhaseExtractor:
        def __init__(self):
            self.call = 0

        def extract_batch(self, chunks, llm=None):
            self.call += 1
            if self.call == 1:
                return [ChunkExtraction(
                    chunk_id=chunks[0]["chunk_id"],
                    entities=[
                        ExtractedEntity(local_id="a", name="支付服务", type="SYSTEM"),
                        ExtractedEntity(local_id="b", name="风控服务", type="SYSTEM"),
                    ],
                    relations=[ExtractedRelation(
                        subject="a", predicate="USES", object="b",
                        evidence=chunks[0]["content"][:20], confidence=0.9,
                    )],
                )]
            return [ChunkExtraction(chunk_id=chunks[0]["chunk_id"], entities=[], relations=[])]

    with SessionLocal() as db:
        document = _make_document(db, path="changeset.txt")
        chunk = _make_chunk(db, document, 0, "支付服务使用风控服务完成交易校验，并通知订单中心。")
        db.commit()

        extractor = TwoPhaseExtractor()
        service = GraphBuildService(db, extractor=extractor)
        first = service.build_graph()
        changes = first["changes"]
        assert changes["changed_entity_ids"]
        assert changes["deleted_entity_ids"] == []
        assert len(changes["changed_relation_ids"]) == 1
        assert set(changes["community_entity_ids"]) >= set(changes["changed_entity_ids"])

        # 第二次全量重抽为空：旧实体/关系被清理，进入 deleted 集合。
        chunk.graph_status = "pending"
        db.commit()
        second = GraphBuildService(db, extractor=extractor).build_graph()
        changes = second["changes"]
        assert changes["changed_entity_ids"] == []
        assert changes["deleted_entity_ids"]
        assert changes["deleted_relation_ids"]
        assert chunk.graph_status == "completed"


# ---------------------------------------------------------------------------
# 3. 防抖聚合
# ---------------------------------------------------------------------------


def _debounce_service(db, queue, monkeypatch, *, debounce=60, min_pending=20, max_wait=300):
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_base import KnowledgeBase
    from app.services.graph_governance_service import GraphGovernanceService

    if db.get(KnowledgeBase, DEFAULT_KNOWLEDGE_BASE_ID) is None:
        db.add(KnowledgeBase.default())
        db.commit()
    monkeypatch.setattr(
        "app.services.graph_governance_service.GRAPH_AUTO_BUILD_DEBOUNCE_SECONDS", debounce
    )
    monkeypatch.setattr(
        "app.services.graph_governance_service.GRAPH_AUTO_BUILD_MIN_PENDING_CHUNKS", min_pending
    )
    monkeypatch.setattr(
        "app.services.graph_governance_service.GRAPH_AUTO_BUILD_MAX_WAIT_SECONDS", max_wait
    )
    return GraphGovernanceService(db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=queue)


def test_automatic_build_debounces_and_pulls_in(db_engine, monkeypatch):
    from datetime import datetime

    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk

    queue = _FakeTaskQueue()
    with SessionLocal() as db:
        document = _make_document(db, path="debounce.txt")
        _make_chunk(db, document, 0, "支付服务使用风控服务完成交易校验，并通知订单中心。")
        db.commit()
        service = _debounce_service(db, queue, monkeypatch)

        first = service.request_automatic_build("user", "job-1")
        assert first is not None
        assert first.visible_at is not None and first.visible_at > datetime.utcnow()
        assert queue.job_ids == [first.id]

        # 防抖窗口内重复触发：合并进同一任务，不提前唤醒。
        second = service.request_automatic_build("user", "job-2")
        assert second.id == first.id
        assert first.visible_at > datetime.utcnow()
        assert queue.job_ids == [first.id]

        # 达到最低 Chunk 门槛：立即拉起。
        for index in range(1, 20):
            _make_chunk(db, document, index, f"第 {index} 段描述了订单服务的依赖关系。")
        db.commit()
        third = service.request_automatic_build("user", "job-3")
        assert third.id == first.id
        assert first.visible_at <= datetime.utcnow()
        assert queue.job_ids == [first.id, first.id]


def test_automatic_build_max_wait_pulls_in(db_engine, monkeypatch):
    from datetime import datetime, timedelta

    from app.core.database import SessionLocal

    queue = _FakeTaskQueue()
    with SessionLocal() as db:
        document = _make_document(db, path="max-wait.txt")
        _make_chunk(db, document, 0, "支付服务使用风控服务完成交易校验，并通知订单中心。")
        db.commit()
        service = _debounce_service(db, queue, monkeypatch)

        job = service.request_automatic_build("user", "job-1")
        assert job.visible_at is not None
        # 模拟任务已等待超过最大时长。
        job.created_at = datetime.utcnow() - timedelta(seconds=301)
        db.commit()

        pulled = service.request_automatic_build("user", "job-2")
        assert pulled.id == job.id
        assert job.visible_at <= datetime.utcnow()


def test_automatic_build_threshold_bypasses_debounce(db_engine, monkeypatch):
    from datetime import datetime

    from app.core.database import SessionLocal

    queue = _FakeTaskQueue()
    with SessionLocal() as db:
        document = _make_document(db, path="bulk.txt")
        for index in range(20):
            _make_chunk(db, document, index, f"第 {index} 段描述了订单服务的依赖关系。")
        db.commit()
        service = _debounce_service(db, queue, monkeypatch)

        job = service.request_automatic_build("user", "job-1")
        assert job is not None
        assert job.visible_at is None
        assert job.options_json["profile"] == "fast"
        assert job.options_json["vector_update"] == "incremental"
        assert job.options_json["community_update"] == "defer"


def test_claim_respects_visible_at(db_engine):
    from datetime import datetime, timedelta

    from app.core.database import SessionLocal
    from app.models.index_job import IndexJob
    from app.repositories.index_job_repository import IndexJobRepository

    with SessionLocal() as db:
        repository = IndexJobRepository(db, None)
        future = IndexJob(
            job_type="sync",
            status="pending",
            total_items=0,
            visible_at=datetime.utcnow() + timedelta(minutes=5),
        )
        due = IndexJob(job_type="rebuild", status="pending", total_items=0)
        db.add_all([future, due])
        db.commit()

        assert repository.claim_next_pending(worker_id="w1").id == due.id
        assert repository.claim(future.id, worker_id="w1", lease_seconds=60) is None
        future.visible_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
        assert repository.claim(future.id, worker_id="w1", lease_seconds=60) is not None


# ---------------------------------------------------------------------------
# 4. 向量增量更新
# ---------------------------------------------------------------------------


def test_entity_vectors_apply_changes_incrementally(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.entity_vector_service import EntityVectorService

    container = _container()
    with SessionLocal() as db:
        document = _make_document(db, path="entity-vector.txt")
        chunk_a = _make_chunk(db, document, 0, "支付服务使用风控服务完成交易校验，并通知订单中心。")
        chunk_b = _make_chunk(db, document, 1, "账务服务使用对账服务完成日终对账，并输出差异报表。")
        db.commit()
        _upsert_fact(db, chunk_a, "支付服务", "风控服务", chunk_a.content[:18])
        _upsert_fact(db, chunk_b, "账务服务", "对账服务", chunk_b.content[:18])

        service = EntityVectorService(db, DEFAULT_KNOWLEDGE_BASE_ID, container=container)
        baseline = service.rebuild()
        assert baseline["status"] == "ready"
        index_name = baseline["index_name"]
        store = container.vector_store.indexes[index_name]
        assert len(store.ids) == 4

        # 新增一个实体：只增量写入，不重建索引。
        chunk_c = _make_chunk(db, document, 2, "清结算服务使用账务服务完成资金清算，并生成流水凭证。")
        db.commit()
        _upsert_fact(db, chunk_c, "清结算服务", "账务服务", chunk_c.content[:18])
        from app.models.knowledge_graph import KgEntity

        changed_ids = [
            item.id for item in db.query(KgEntity).all()
            if item.canonical_name in {"清结算服务", "账务服务"}
        ]
        result = service.apply_changes(changed_ids, [])
        assert result["status"] == "incremental"
        assert result["index_name"] == index_name
        assert result["entity_count"] == 5
        assert len(store.ids) == 5
        assert len(set(store.ids)) == 5

        # 删除实体：只删除对应向量。
        settlement = db.query(KgEntity).filter(
            KgEntity.canonical_name == "清结算服务"
        ).one()
        from rag.knowledge_graph.schemas import ChunkExtraction
        from rag.knowledge_graph.sqlite_store import SqliteGraphStore

        SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID).upsert_extraction(
            {"document_id": document.id, "chunk_id": chunk_c.id},
            ChunkExtraction(chunk_id=chunk_c.id, entities=[], relations=[]),
        )
        SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID).cleanup_orphans()
        result = service.apply_changes([], [settlement.id])
        assert result["status"] == "incremental"
        assert result["deleted"] == 1
        assert result["entity_count"] == 4
        assert len(store.ids) == 4


def test_relation_vectors_apply_changes_incrementally(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.relation_vector_service import RelationVectorService

    container = _container()
    with SessionLocal() as db:
        document = _make_document(db, path="relation-vector.txt")
        chunk_a = _make_chunk(db, document, 0, "支付网关依赖风控引擎完成实时交易校验。")
        db.commit()
        _upsert_fact(db, chunk_a, "支付网关", "风控引擎", chunk_a.content[:16], predicate="DEPENDS_ON")

        service = RelationVectorService(db, DEFAULT_KNOWLEDGE_BASE_ID, container=container)
        baseline = service.rebuild()
        assert baseline["relation_count"] == 1
        index_name = baseline["index_name"]
        store = container.vector_store.indexes[index_name]

        chunk_b = _make_chunk(db, document, 1, "支付网关调用清结算中心完成资金划拨。")
        db.commit()
        _upsert_fact(db, chunk_b, "支付网关", "清结算中心", chunk_b.content[:16], predicate="CALLS")

        from app.models.knowledge_graph import KgRelation

        changed = [
            item.id for item in db.query(KgRelation).all()
            if item.predicate in {"CALLS", "DEPENDS_ON"}
        ]
        result = service.apply_changes(changed, [])
        assert result["status"] == "incremental"
        assert result["index_name"] == index_name
        assert result["relation_count"] == 2
        assert len(store.ids) == 2

        # 空变化：直接返回 unchanged，不触碰向量库。
        unchanged = service.apply_changes([], [])
        assert unchanged["status"] == "unchanged"


# ---------------------------------------------------------------------------
# 5. 社区状态机 + 受影响子图重建
# ---------------------------------------------------------------------------


def test_community_state_and_affected_rebuild(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.graph_governance import GraphCommunity
    from app.services.graph_governance_service import GraphGovernanceService

    with SessionLocal() as db:
        document = _make_document(db, path="community.txt")
        chunk_1 = _make_chunk(db, document, 0, "支付服务使用风控服务完成交易校验。")
        chunk_2 = _make_chunk(db, document, 1, "账务服务使用对账服务完成日终对账。")
        db.commit()
        _upsert_fact(db, chunk_1, "支付服务", "风控服务", chunk_1.content[:14])
        _upsert_fact(db, chunk_2, "账务服务", "对账服务", chunk_2.content[:14])

        governance = GraphGovernanceService(db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=object())
        full = governance.rebuild_communities()
        assert full == {"communities": 2, "entities": 4}
        assert governance.community_state().status == "ready"
        assert governance.community_state().communities == 2

        # 图变化后标记 stale：图谱仍可检索，社区延迟重建。
        governance.mark_communities_stale()
        assert governance.community_state().status == "stale"

        # 桥接两个连通分量：只重建受影响部分，另一侧不动。
        from app.models.knowledge_graph import KgEntity

        payment = db.query(KgEntity).filter(
            KgEntity.canonical_name == "支付服务"
        ).one()
        audit = db.query(KgEntity).filter(
            KgEntity.canonical_name == "账务服务"
        ).one()
        bridge = _make_chunk(db, document, 2, "支付服务调用账务服务完成资金流转。")
        db.commit()
        _upsert_fact(db, bridge, "支付服务", "账务服务", bridge.content[:14], predicate="CALLS")

        affected = governance.rebuild_communities(
            changed_entity_ids=[payment.id, audit.id]
        )
        assert affected == {"communities": 1, "entities": 4}
        assert governance.community_state().status == "ready"
        level0 = db.query(GraphCommunity).filter(GraphCommunity.level == 0).all()
        assert len(level0) == 1


def test_graph_maintenance_enqueues_stale_knowledge_bases(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_base import KnowledgeBase
    from app.services.graph_maintenance import enqueue_stale_community_rebuilds
    from app.services.graph_governance_service import GraphGovernanceService
    from app.bootstrap import container as container_module

    container = _container()
    container.metadata = SimpleNamespace(session=SessionLocal)
    monkeypatch.setattr(
        "app.services.graph_maintenance.GRAPH_COMMUNITY_MAINTENANCE_ENABLED", True
    )
    monkeypatch.setattr(
        container_module, "get_provider_container", lambda: container
    )
    with SessionLocal() as db:
        if db.get(KnowledgeBase, DEFAULT_KNOWLEDGE_BASE_ID) is None:
            db.add(KnowledgeBase.default())
            db.commit()
        governance = GraphGovernanceService(db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=object())
        governance.mark_communities_stale()

        queued = enqueue_stale_community_rebuilds()
        assert queued == 1
        assert container.task_queue.job_ids

        from app.models.index_job import IndexJob

        job = db.query(IndexJob).filter(IndexJob.job_type == "graph_maintenance").one()
        assert job.options_json["trigger"] == "community_maintenance"
        assert enqueue_stale_community_rebuilds() == 0  # 去重


# ---------------------------------------------------------------------------
# 6. Schema 差异化重建
# ---------------------------------------------------------------------------


def test_schema_type_removal_prunes_without_reextraction(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_graph import KgEntity
    from app.services.graph_governance_service import GraphGovernanceService

    with SessionLocal() as db:
        document = _make_document(db, path="schema-removal.txt")
        chunk = _make_chunk(db, document, 0, "支付服务使用风控服务完成交易校验。", graph_status="completed")
        db.commit()
        _upsert_fact(db, chunk, "支付服务", "风控服务", chunk.content[:14])

        governance = GraphGovernanceService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=_FakeTaskQueue()
        )
        active = governance.active_schema()
        # 激活一个去掉 SYSTEM 类型的 Schema。
        draft = governance.create_schema(
            entity_types=[t for t in active.entity_types_json if t != "SYSTEM"],
            relation_types=active.relation_types_json,
            normalization_rules=active.normalization_rules_json or {},
            extraction_prompt=None,
            created_by=None,
        )
        estimate = governance.estimate_schema_rebuild(draft.id)
        assert estimate["change_kind"] == "type_removal"
        assert estimate["estimated_llm_requests"] == 0
        assert set(estimate["removed_entity_types"]) == {"SYSTEM"}

        governance.activate_schema(draft.id)

        assert db.query(KgEntity).count() == 0  # SYSTEM 实体被库内清理
        statuses = {
            c.graph_status for c in db.query(KnowledgeChunk).all()
        }
        assert statuses == {"completed"}  # 无需重抽
        versions = {c.graph_schema_version for c in db.query(KnowledgeChunk).all()}
        assert versions == {draft.version}
        assert governance.community_state().status == "stale"


def test_schema_type_addition_marks_only_matching_chunks(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.services.graph_governance_service import GraphGovernanceService

    with SessionLocal() as db:
        document = _make_document(db, path="schema-addition.txt")
        matching = _make_chunk(
            db, document, 0,
            "系统对外暴露 api_endpoint 网关，用于承接第三方回调请求。",
            graph_status="completed",
        )
        unrelated = _make_chunk(
            db, document, 1,
            "账务服务每日凌晨执行日终对账，并将差异写入报表中心。",
            graph_status="completed",
        )
        db.commit()

        governance = GraphGovernanceService(
            db, DEFAULT_KNOWLEDGE_BASE_ID, task_queue=_FakeTaskQueue()
        )
        active = governance.active_schema()
        draft = governance.create_schema(
            entity_types=sorted(set(active.entity_types_json) | {"API_ENDPOINT"}),
            relation_types=active.relation_types_json,
            normalization_rules=active.normalization_rules_json or {},
            extraction_prompt=None,
            created_by=None,
        )
        estimate = governance.estimate_schema_rebuild(draft.id)
        assert estimate["change_kind"] == "type_addition"
        assert estimate["affected_chunks"] == 1

        governance.activate_schema(draft.id)

        db.refresh(matching)
        db.refresh(unrelated)
        assert matching.graph_status == "pending"
        assert unrelated.graph_status == "completed"
        assert unrelated.graph_schema_version == draft.version


# ---------------------------------------------------------------------------
# 7. 两阶段抽取升级
# ---------------------------------------------------------------------------


def test_escalation_upgrades_empty_extractions(db_engine):
    from app.core.database import SessionLocal
    from app.services.knowledge_graph_service import GraphBuildService
    from rag.knowledge_graph.schemas import ChunkExtraction, ExtractedEntity, ExtractedRelation

    class StagedExtractor:
        def __init__(self):
            self.calls = []

        def configure_schema(self, **kwargs):
            pass

        def extract_batch(self, chunks, llm=None):
            self.calls.append(llm)
            if llm is None:
                return [
                    ChunkExtraction(chunk_id=c["chunk_id"], entities=[], relations=[])
                    for c in chunks
                ]
            return [
                ChunkExtraction(
                    chunk_id=c["chunk_id"],
                    entities=[
                        ExtractedEntity(local_id="a", name="支付服务", type="SYSTEM"),
                        ExtractedEntity(local_id="b", name="风控服务", type="SYSTEM"),
                    ],
                    relations=[ExtractedRelation(
                        subject="a", predicate="USES", object="b",
                        evidence=c["content"][:20], confidence=0.9,
                    )],
                )
                for c in chunks
            ]

    class FakeEscalationLLM:
        def identity(self):
            from rag.providers.interfaces import ProviderIdentity

            return ProviderIdentity(kind="llm", provider="fake", model="strong-model")

    with SessionLocal() as db:
        document = _make_document(db, path="escalation.txt")
        chunk = _make_chunk(db, document, 0, "支付服务使用风控服务完成交易校验，并通知订单中心。")
        db.commit()

        extractor = StagedExtractor()
        service = GraphBuildService(db, extractor=extractor)
        service._escalation_loaded = True
        service._escalation_provider = FakeEscalationLLM()

        result = service.build_graph()

        assert extractor.calls[0] is None and extractor.calls[1] is not None
        assert result["succeeded"] == 1
        assert result["graph_stats"]["entities"] == 2
        assert chunk.graph_status == "completed"
        assert result["changes"]["changed_entity_ids"]
