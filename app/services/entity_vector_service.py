"""Provider-neutral semantic entity index kept separate from document vectors."""

from __future__ import annotations

import hashlib
import json

from langchain_core.documents import Document
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import INDEX_JOB_MAX_ATTEMPTS
from app.models.code_graph import EntityVectorIndexState
from app.models.index_job import IndexJob
from app.repositories.index_job_repository import IndexJobRepository
from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from rag.providers.interfaces import VectorIndexRef


class EntityVectorService:
    def __init__(self, db: Session, knowledge_base_id: str, container=None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        if container is None:
            from app.bootstrap.container import get_provider_container
            container = get_provider_container()
        self.container = container

    def state(self) -> EntityVectorIndexState | None:
        return self.db.scalar(select(EntityVectorIndexState).where(
            EntityVectorIndexState.knowledge_base_id == self.knowledge_base_id
        ))

    def request_rebuild(self, requested_by: str | None) -> IndexJob:
        repository = IndexJobRepository(self.db, self.knowledge_base_id)
        existing = repository.find_open("entity_vector_rebuild")
        if existing is not None:
            return existing
        job = repository.create(IndexJob(
            job_type="entity_vector_rebuild",
            status="pending",
            requested_by=requested_by,
            total_items=1,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
        ))
        self.container.task_queue.enqueue(job.id)
        return job

    def rebuild(self) -> dict:
        repository = KnowledgeGraphRepository(self.db, self.knowledge_base_id)
        entities = repository.list_entities()
        embedding_identity = self.container.embedding.identity().as_dict()
        embedding_fingerprint = json.dumps(embedding_identity, sort_keys=True, ensure_ascii=False)
        graph_fingerprint = hashlib.sha256("\n".join(
            f"{entity.id}:{entity.updated_at}:{entity.canonical_name}:{entity.description or ''}"
            for entity in entities
        ).encode("utf-8")).hexdigest()
        state = self.state()
        if (
            state and state.status == "ready"
            and state.embedding_fingerprint == embedding_fingerprint
            and state.graph_fingerprint == graph_fingerprint
        ):
            return {"status": "unchanged", "entity_count": state.entity_count, "index_name": state.index_name}

        old_index_name = state.index_name if state else None
        if state is None:
            state = EntityVectorIndexState(knowledge_base_id=self.knowledge_base_id)
            self.db.add(state)
        state.status = "building"
        self.db.commit()

        compact_kb = self.knowledge_base_id.replace("-", "")[:12]
        index_name = f"kg_entities_{compact_kb}_{graph_fingerprint[:12]}"
        index_ref = VectorIndexRef(
            name=index_name,
            knowledge_base_id=self.knowledge_base_id,
            index_version=graph_fingerprint[:12],
            embedding_profile_id=hashlib.sha256(embedding_fingerprint.encode()).hexdigest()[:12],
        )
        # A deterministic name may remain after an interrupted build; recreate
        # the staging index so retries are idempotent.
        if index_name != old_index_name:
            try:
                self.container.vector_store.delete_index(index_ref)
            except Exception:
                pass
        store = self.container.vector_store.open(self.container.embedding.get_model(), index_ref)
        documents = []
        ids = []
        for entity in entities:
            aliases = [alias.alias for alias in repository.get_entity_aliases(entity.id)]
            documents.append(Document(
                page_content="\n".join(filter(None, [
                    entity.canonical_name,
                    "、".join(aliases),
                    entity.description,
                    f"类型：{entity.entity_type}",
                ])),
                metadata={
                    "entity_id": entity.id,
                    "knowledge_base_id": self.knowledge_base_id,
                    "entity_type": entity.entity_type,
                    "canonical_name": entity.canonical_name,
                },
            ))
            ids.append(f"entity:{entity.id}")
        try:
            if documents:
                store.add_documents(documents, ids=ids)
            state.index_name = index_name
            state.embedding_fingerprint = embedding_fingerprint
            state.graph_fingerprint = graph_fingerprint
            state.entity_count = len(entities)
            state.status = "ready"
            self.db.commit()
        except Exception:
            state.status = "failed"
            self.db.commit()
            raise
        if old_index_name and old_index_name != index_name:
            try:
                self.container.vector_store.delete_index(VectorIndexRef(
                    name=old_index_name,
                    knowledge_base_id=self.knowledge_base_id,
                ))
            except Exception:
                pass
        return {"status": "ready", "entity_count": len(entities), "index_name": index_name}

    def semantic_search(self, query: str, limit: int = 10) -> list[dict]:
        state = self.state()
        if state is None or state.status != "ready" or not state.index_name:
            return []
        store = self.container.vector_store.open(
            self.container.embedding.get_model(),
            VectorIndexRef(name=state.index_name, knowledge_base_id=self.knowledge_base_id),
        )
        try:
            rows = store.similarity_search_with_relevance_scores(query, k=limit)
            return [{**doc.metadata, "match_score": float(score)} for doc, score in rows]
        except (AttributeError, NotImplementedError):
            docs = store.similarity_search(query, k=limit)
            return [{**doc.metadata, "match_score": 1.0 / (index + 1)} for index, doc in enumerate(docs)]
