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
    KNOWLEDGE_GRAPH_ENABLED,
)
from app.core.database import SessionLocal
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from rag.knowledge_graph.normalizer import normalize_name

logger = logging.getLogger(__name__)


class KnowledgeGraphRetriever:
    """知识图谱检索器。

    1. 种子实体匹配（精确/包含）
    2. 最多二跳图遍历
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

    def _retrieve(self, query: str, top_k: int) -> list[Document]:
        from app.bootstrap.container import get_provider_container

        store = get_provider_container().graph_store(
            self.db,
            self.knowledge_base_id,
        )

        # Step 1: seed entity matching
        entities = store.search_entities(query, limit=GRAPH_ENTITY_SEARCH_K)
        try:
            from app.core.config import GRAPH_ENTITY_VECTOR_ENABLED
            from app.services.entity_vector_service import EntityVectorService

            semantic = (
                EntityVectorService(self.db, self.knowledge_base_id).semantic_search(
                    query, GRAPH_ENTITY_SEARCH_K
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
                    from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository

                    model = KnowledgeGraphRepository(
                        self.db, self.knowledge_base_id
                    ).get_entity(entity_id)
                    if model:
                        by_id[entity_id] = {
                            "id": model.id,
                            "entity_type": model.entity_type,
                            "canonical_name": model.canonical_name,
                            "normalized_name": model.normalized_name,
                            "description": model.description,
                            "aliases": [],
                            "match_score": float(item.get("match_score", 0)),
                        }
            entities = sorted(
                by_id.values(), key=lambda item: float(item.get("match_score", 0)), reverse=True
            )[:GRAPH_ENTITY_SEARCH_K]
        except Exception as exc:
            logger.debug("Semantic entity retrieval unavailable: %s", exc)
        if not entities:
            return []

        entity_ids = [e["id"] for e in entities]
        seed_scores = {}
        for i, ent in enumerate(entities):
            seed_scores[ent["id"]] = ent.get("match_score", 1.0 / (i + 1))

        # Step 2: graph traversal
        relations = store.get_neighbors(entity_ids, GRAPH_MAX_HOPS, GRAPH_MAX_RELATIONS)
        if not relations:
            return []

        # Step 3: map back to chunks via evidence
        relation_ids = [r["relation_id"] for r in relations]
        evidence = store.get_relation_evidence(relation_ids)

        # Build chunk -> graph_facts mapping
        chunk_facts: dict[str, list[str]] = {}
        chunk_scores: dict[str, float] = {}

        for ev in evidence:
            cid = ev["chunk_id"]
            if cid not in chunk_facts:
                chunk_facts[cid] = []

            rel = next((r for r in relations if r["relation_id"] == ev["relation_id"]), None)
            if rel:
                fact = f"{rel['subject_name']} --{rel['predicate']}--> {rel['object_name']}"
                if fact not in chunk_facts[cid]:
                    chunk_facts[cid].append(fact)

                # Score: seed_score * decay^hop * confidence
                hop = rel["hop"]
                subj_score = seed_scores.get(rel["subject_id"], 0.5)
                graph_score = subj_score * math.pow(0.75, hop) * rel["confidence"]
                chunk_scores[cid] = max(chunk_scores.get(cid, 0), graph_score)

        if not chunk_facts:
            return []

        # Step 4: load chunks from SQLite
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
