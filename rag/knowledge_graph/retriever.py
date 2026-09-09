"""知识图谱检索器。

通过实体匹配和图遍历获取相关片段。
"""

from __future__ import annotations

import logging
import math
from typing import Any

from langchain_core.documents import Document

from app.core.config import (
    GRAPH_ENTITY_SEARCH_K,
    GRAPH_MAX_HOPS,
    GRAPH_MAX_RELATIONS,
    GRAPH_RELATION_SEARCH_K,
    KNOWLEDGE_GRAPH_ENABLED,
)
from app.core.database import SessionLocal
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from rag.knowledge_graph.normalizer import normalize_name
from rag.retrieval_strategy import get_retrieval_policy

logger = logging.getLogger(__name__)


class KnowledgeGraphRetriever:
    """知识图谱检索器。

    1. 实体词法/向量匹配与关系词法/向量匹配
    2. 以实体和关系端点为种子进行最多二跳图遍历
    3. 映射回原始 chunk
    """

    def __init__(
        self,
        db=None,
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    ):
        self._db = db
        self._owns_db = False
        self.knowledge_base_id = knowledge_base_id

    @property
    def db(self):
        if self._db is None:
            self._db = SessionLocal()
            self._owns_db = True
        return self._db

    def get_relevant_documents(self, query: str, top_k: int = 8) -> list[Document]:
        if not KNOWLEDGE_GRAPH_ENABLED:
            return []

        try:
            return self._retrieve(query, top_k)
        except Exception as exc:
            logger.warning("Graph retrieval failed: %s", exc)
            return []

    def retrieve_with_options(
        self,
        queries: list[str],
        *,
        query_embedding: list[float] | None = None,
        strategy: str = "standard",
        allowed_document_ids: set[str] | None = None,
    ) -> list[Document]:
        policy = get_retrieval_policy(strategy)
        query = " ".join(dict.fromkeys(queries))
        docs = self._retrieve(
            query,
            policy.graph_top_k,
            query_embedding=query_embedding,
            strategy=strategy,
        )
        if allowed_document_ids is None:
            return docs
        return [
            doc for doc in docs
            if doc.metadata.get("document_id") in allowed_document_ids
        ]

    def _retrieve(
        self,
        query: str,
        top_k: int,
        *,
        query_embedding: list[float] | None = None,
        strategy: str = "standard",
    ) -> list[Document]:
        from app.bootstrap.container import get_provider_container

        policy = get_retrieval_policy(strategy)

        store = get_provider_container().graph_store(
            self.db,
            self.knowledge_base_id,
        )

        # Step 1: entity seeds from lexical and semantic matching.
        entities = store.search_entities(query, limit=policy.graph_entity_k)
        try:
            from app.core.config import GRAPH_ENTITY_VECTOR_ENABLED
            from app.services.entity_vector_service import EntityVectorService

            semantic = (
                EntityVectorService(self.db, self.knowledge_base_id).semantic_search(
                    query,
                    policy.graph_entity_k,
                    query_embedding=query_embedding,
                )
                if GRAPH_ENTITY_VECTOR_ENABLED else []
            )
            by_id = {entity["id"]: entity for entity in entities}
            for item in semantic:
                entity_id = item.get("entity_id")
                if not entity_id:
                    continue
                if entity_id in by_id:
                    by_id[entity_id]["match_score"] = max(
                        float(by_id[entity_id].get("match_score", 0)),
                        float(item.get("match_score", 0)),
                    )
                else:
                    model = next((
                        candidate for candidate in store.list_effective_entities()
                        if candidate["id"] == entity_id
                    ), None)
                    if model:
                        by_id[entity_id] = {
                            **model,
                            "match_score": float(item.get("match_score", 0)),
                        }
            entities = sorted(
                by_id.values(), key=lambda item: float(item.get("match_score", 0)), reverse=True
            )[:policy.graph_entity_k]
        except Exception as exc:
            logger.debug("Semantic entity retrieval unavailable: %s", exc)

        # Step 2: relation seeds from predicate lexical and semantic matching.
        relation_seeds = store.search_relations(
            query, limit=policy.graph_relation_k
        )
        for relation in relation_seeds:
            relation["match_methods"] = [
                relation.get("match_method", "relation_lexical")
            ]
        try:
            from app.core.config import GRAPH_RELATION_VECTOR_ENABLED
            from app.services.relation_vector_service import RelationVectorService

            semantic_relations = (
                RelationVectorService(
                    self.db, self.knowledge_base_id
                ).semantic_search(
                    query,
                    policy.graph_relation_k,
                    query_embedding=query_embedding,
                )
                if GRAPH_RELATION_VECTOR_ENABLED else []
            )
            semantic_scores = {
                item["relation_id"]: float(item.get("match_score", 0))
                for item in semantic_relations
                if item.get("relation_id")
            }
            hydrated = store.get_relations(list(semantic_scores))
            by_id = {item["relation_id"]: item for item in relation_seeds}
            for item in hydrated:
                relation_id = item["relation_id"]
                score = semantic_scores.get(relation_id, 0.0)
                if relation_id in by_id:
                    by_id[relation_id]["match_score"] = max(
                        float(by_id[relation_id].get("match_score", 0)), score
                    )
                    methods = set(by_id[relation_id].get("match_methods", []))
                    methods.add("relation_vector")
                    by_id[relation_id]["match_methods"] = sorted(methods)
                else:
                    by_id[relation_id] = {
                        **item,
                        "match_score": score,
                        "match_methods": ["relation_vector"],
                    }
            relation_seeds = sorted(
                by_id.values(),
                key=lambda item: float(item.get("match_score", 0)),
                reverse=True,
            )[:policy.graph_relation_k]
        except Exception as exc:
            logger.debug("Semantic relation retrieval unavailable: %s", exc)

        if not entities and not relation_seeds:
            return []

        seed_scores: dict[str, float] = {}
        for i, ent in enumerate(entities):
            seed_scores[ent["id"]] = ent.get("match_score", 1.0 / (i + 1))

        relation_seed_scores: dict[str, float] = {}
        entity_ids = [entity["id"] for entity in entities]
        matched_entity_ids = list(entity_ids)
        for index, relation in enumerate(relation_seeds):
            score = max(
                0.0,
                float(relation.get("match_score", 1.0 / (index + 1))),
            )
            relation_seed_scores[relation["relation_id"]] = score
            for entity_id in (relation["subject_id"], relation["object_id"]):
                seed_scores[entity_id] = max(seed_scores.get(entity_id, 0.0), score)
                entity_ids.append(entity_id)
        entity_ids = list(dict.fromkeys(entity_ids))

        # Step 3: traverse from both entity matches and relation endpoints.
        traversed = store.get_neighbors(
            entity_ids, policy.graph_max_hops, policy.graph_max_relations
        )
        relations_by_id = {
            relation["relation_id"]: {**relation, "hop": 0}
            for relation in relation_seeds
        }
        for relation in traversed:
            relations_by_id.setdefault(relation["relation_id"], relation)
        relations = list(relations_by_id.values())[:policy.graph_max_relations]

        # Step 4: map relations and entity mentions back to real source chunks.
        relation_ids = [r["relation_id"] for r in relations]
        evidence = store.get_relation_evidence(relation_ids)
        entity_evidence = store.get_entity_evidence(matched_entity_ids)

        # Build chunk -> graph_facts mapping
        chunk_facts: dict[str, list[str]] = {}
        chunk_scores: dict[str, float] = {}
        chunk_match_methods: dict[str, set[str]] = {}

        for ev in evidence:
            cid = ev["chunk_id"]
            if cid not in chunk_facts:
                chunk_facts[cid] = []
                chunk_match_methods[cid] = set()

            rel = next((r for r in relations if r["relation_id"] == ev["relation_id"]), None)
            if rel:
                fact = f"{rel['subject_name']} --{rel['predicate']}--> {rel['object_name']}"
                if fact not in chunk_facts[cid]:
                    chunk_facts[cid].append(fact)
                chunk_match_methods[cid].update(
                    rel.get("match_methods", ["graph_traversal"])
                )

                hop = int(rel["hop"])
                direct_score = relation_seed_scores.get(rel["relation_id"])
                endpoint_score = max(
                    seed_scores.get(rel["subject_id"], 0.0),
                    seed_scores.get(rel["object_id"], 0.0),
                    0.5,
                )
                graph_score = (
                    direct_score if direct_score is not None
                    else endpoint_score * math.pow(0.75, hop)
                ) * rel["confidence"]
                chunk_scores[cid] = max(chunk_scores.get(cid, 0), graph_score)

        for ev in entity_evidence:
            cid = ev["chunk_id"]
            if cid not in chunk_facts:
                chunk_facts[cid] = []
                chunk_match_methods[cid] = set()
            fact = f"实体：{ev['entity_name']}（{ev['entity_type']}）"
            if fact not in chunk_facts[cid]:
                chunk_facts[cid].append(fact)
            chunk_match_methods[cid].add("entity_mention")
            entity_score = max(
                float(seed_scores.get(ev["entity_id"], 0.5)), 0.5
            ) * float(ev["confidence"])
            chunk_scores[cid] = max(chunk_scores.get(cid, 0), entity_score)

        if not chunk_facts:
            return []

        # Step 5: load chunks from SQLite.
        from app.models.knowledge_chunk import KnowledgeChunk
        from sqlalchemy import select

        chunks = list(self.db.scalars(
            select(KnowledgeChunk).where(
                KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                KnowledgeChunk.id.in_(list(chunk_facts.keys())),
            )
        ))

        # Build documents
        doc_map: dict[str, KnowledgeChunk] = {c.id: c for c in chunks}
        results: list[Document] = []

        sorted_cids = sorted(chunk_scores.keys(), key=lambda c: chunk_scores[c], reverse=True)
        for cid in sorted_cids[:top_k]:
            chunk = doc_map.get(cid)
            if not chunk:
                continue

            doc = Document(
                page_content=chunk.content,
                metadata={
                    "chunk_id": chunk.id,
                    "knowledge_base_id": self.knowledge_base_id,
                    "document_id": chunk.document_id,
                    "source": _get_source_path(
                        self.db,
                        chunk.document_id,
                        self.knowledge_base_id,
                    ),
                    "file_hash": chunk.file_hash,
                    "chunk_index": chunk.chunk_index,
                    "page": chunk.page,
                    "section": chunk.section,
                    "content_hash": chunk.content_hash,
                    "graph_facts": chunk_facts.get(cid, []),
                    "graph_score": chunk_scores[cid],
                    "graph_match_methods": sorted(chunk_match_methods.get(cid, set())),
                    "_channel": "graph",
                },
            )
            results.append(doc)

        return results


def _get_source_path(db, document_id: str, knowledge_base_id: str) -> str:
    from app.models.knowledge_document import KnowledgeDocument
    from sqlalchemy import select

    doc = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
        )
    )
    return doc.source_path if doc else "unknown"


_retrievers_by_knowledge_base: dict[str, KnowledgeGraphRetriever] = {}


def get_graph_retriever(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> KnowledgeGraphRetriever:
    if knowledge_base_id not in _retrievers_by_knowledge_base:
        _retrievers_by_knowledge_base[knowledge_base_id] = KnowledgeGraphRetriever(
            knowledge_base_id=knowledge_base_id
        )
    return _retrievers_by_knowledge_base[knowledge_base_id]


def invalidate_graph_retrievers(knowledge_base_id: str | None = None) -> None:
    targets = (
        list(_retrievers_by_knowledge_base)
        if knowledge_base_id is None
        else [knowledge_base_id]
    )
    for target in targets:
        retriever = _retrievers_by_knowledge_base.pop(target, None)
        if (
            retriever is not None
            and retriever._db is not None
            and retriever._owns_db
        ):
            retriever._db.close()
