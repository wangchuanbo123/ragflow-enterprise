"""Provider-neutral semantic entity index kept separate from document vectors."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import INDEX_JOB_MAX_ATTEMPTS, INGEST_BATCH_SIZE
from app.models.code_graph import EntityVectorIndexState
from app.models.index_job import IndexJob
from app.repositories.index_job_repository import IndexJobRepository, signal_persisted_job
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

    def request_rebuild(
        self, requested_by: str | None, task_queue=None
    ) -> IndexJob:
        repository = IndexJobRepository(self.db, self.knowledge_base_id)
        existing = repository.find_open("entity_vector_rebuild")
        # A pending job will read the newest graph when it starts. If the only
        # open job is already running, queue one follow-up so an edit that lands
        # after its snapshot cannot be lost.
        if existing is not None and existing.status == "pending":
            return existing
        job = repository.create(IndexJob(
            job_type="entity_vector_rebuild",
            status="pending",
            requested_by=requested_by,
            total_items=1,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
        ))
        queue = task_queue or self.container.task_queue
        signal_persisted_job(queue, job.id)
        return job

    def _entity_document(self, entity, aliases: list[str]):
        from langchain_core.documents import Document

        def value(name: str):
            return entity.get(name) if isinstance(entity, dict) else getattr(entity, name)

        return Document(
            page_content="\n".join(filter(None, [
                value("canonical_name"),
                "、".join(aliases),
                value("description"),
                f"类型：{value('entity_type')}",
            ])),
            metadata={
                "entity_id": value("id"),
                "knowledge_base_id": self.knowledge_base_id,
                "entity_type": value("entity_type"),
                "canonical_name": value("canonical_name"),
            },
        )

    def apply_changes(
        self,
        changed_entity_ids: list[str] | None,
        deleted_entity_ids: list[str] | None,
    ) -> dict:
        """Incrementally upsert/delete vectors; fall back to a full rebuild
        whenever the incremental baseline (ready index, same embedding) is gone."""
        changed = list(dict.fromkeys(changed_entity_ids or []))
        deleted = list(dict.fromkeys(deleted_entity_ids or []))
        if not changed and not deleted:
            state = self.state()
            if state is not None and state.status == "ready":
                return {
                    "status": "unchanged",
                    "entity_count": state.entity_count,
                    "index_name": state.index_name,
                }
            return self.rebuild()

        embedding_identity = self.container.embedding.identity().as_dict()
        embedding_fingerprint = json.dumps(
            embedding_identity, sort_keys=True, ensure_ascii=False
        )
        state = self.state()
        if (
            state is None
            or state.status != "ready"
            or not state.index_name
            or state.embedding_fingerprint != embedding_fingerprint
        ):
            return self.rebuild()

        repository = KnowledgeGraphRepository(self.db, self.knowledge_base_id)
        index_ref = VectorIndexRef(
            name=state.index_name,
            knowledge_base_id=self.knowledge_base_id,
        )
        deleted_set = set(deleted)
        try:
            if deleted:
                self.container.vector_store.delete_vectors(
                    index_ref,
                    ids=[f"entity:{entity_id}" for entity_id in deleted],
                )
            documents = []
            ids = []
            stale: list[str] = []
            for entity_id in changed:
                if entity_id in deleted_set:
                    continue
                entity = repository.get_entity(entity_id)
                if entity is None:
                    stale.append(entity_id)
                    continue
                aliases = [alias.alias for alias in repository.get_entity_aliases(entity.id)]
                documents.append(self._entity_document(entity, aliases))
                ids.append(f"entity:{entity.id}")
            if stale:
                self.container.vector_store.delete_vectors(
                    index_ref, ids=[f"entity:{item}" for item in stale]
                )
            if documents:
                store = self.container.vector_store.open(
                    self.container.embedding.get_model(), index_ref
                )
                for start in range(0, len(documents), INGEST_BATCH_SIZE):
                    store.add_documents(
                        documents[start:start + INGEST_BATCH_SIZE],
                        ids=ids[start:start + INGEST_BATCH_SIZE],
                    )
            state.entity_count = repository.count_entities()
            state.graph_fingerprint = ""
            state.status = "ready"
            self.db.commit()
        except Exception:
            state.status = "failed"
            self.db.commit()
            raise
        return {
            "status": "incremental",
            "updated": len(ids),
            "deleted": len(deleted) + len(stale),
            "entity_count": state.entity_count,
            "index_name": state.index_name,
        }

    def rebuild(self) -> dict:
        from rag.knowledge_graph.sqlite_store import SqliteGraphStore

        entities = SqliteGraphStore(
            self.db, self.knowledge_base_id
        ).list_effective_entities()
        embedding_identity = self.container.embedding.identity().as_dict()
        embedding_fingerprint = json.dumps(embedding_identity, sort_keys=True, ensure_ascii=False)
        graph_fingerprint = hashlib.sha256("\n".join(
            f"{entity['id']}:{entity['updated_at']}:{entity['canonical_name']}:{entity['description'] or ''}:"
            f"{','.join(entity['aliases'])}"
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
        embedding_hash = hashlib.sha256(embedding_fingerprint.encode()).hexdigest()[:12]
        index_name = f"kg_entities_{compact_kb}_{embedding_hash}_{graph_fingerprint[:12]}"
        index_ref = VectorIndexRef(
            name=index_name,
            knowledge_base_id=self.knowledge_base_id,
            index_version=graph_fingerprint[:12],
            embedding_profile_id=embedding_hash,
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
            documents.append(self._entity_document(entity, entity["aliases"]))
            ids.append(f"entity:{entity['id']}")
        try:
            for start in range(0, len(documents), INGEST_BATCH_SIZE):
                store.add_documents(
                    documents[start:start + INGEST_BATCH_SIZE],
                    ids=ids[start:start + INGEST_BATCH_SIZE],
                )
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

    def semantic_search(
        self,
        query: str,
        limit: int = 10,
        query_embedding: list[float] | None = None,
    ) -> list[dict]:
        state = self.state()
        if state is None or state.status != "ready" or not state.index_name:
            return []
        store = self.container.vector_store.open(
            self.container.embedding.get_model(),
            VectorIndexRef(name=state.index_name, knowledge_base_id=self.knowledge_base_id),
        )
        try:
            if query_embedding is not None:
                rows = store.similarity_search_by_vector_with_relevance_scores(
                    query_embedding, k=limit
                )
            else:
                rows = store.similarity_search_with_relevance_scores(query, k=limit)
            return [{**doc.metadata, "match_score": float(score)} for doc, score in rows]
        except (AttributeError, NotImplementedError):
            docs = (
                store.similarity_search_by_vector(query_embedding, k=limit)
                if query_embedding is not None
                else store.similarity_search(query, k=limit)
            )
            return [{**doc.metadata, "match_score": 1.0 / (index + 1)} for index, doc in enumerate(docs)]
