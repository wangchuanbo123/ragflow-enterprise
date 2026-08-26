"""Provider-neutral semantic relation index, isolated from document vectors."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import INDEX_JOB_MAX_ATTEMPTS, INGEST_BATCH_SIZE
from app.models.code_graph import RelationVectorIndexState
from app.models.index_job import IndexJob
from app.models.knowledge_graph import KgRelation
from app.repositories.index_job_repository import IndexJobRepository
from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from rag.knowledge_graph.relation_vocabulary import (
    relation_description,
    relation_terms,
)
from rag.providers.interfaces import VectorIndexRef


class RelationVectorService:
    def __init__(self, db: Session, knowledge_base_id: str, container=None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        if container is None:
            from app.bootstrap.container import get_provider_container

            container = get_provider_container()
        self.container = container

    def state(self) -> RelationVectorIndexState | None:
        return self.db.scalar(select(RelationVectorIndexState).where(
            RelationVectorIndexState.knowledge_base_id == self.knowledge_base_id
        ))

    def request_rebuild(
        self, requested_by: str | None, task_queue=None
    ) -> IndexJob:
        repository = IndexJobRepository(self.db, self.knowledge_base_id)
        existing = repository.find_open("relation_vector_rebuild")
        if existing is not None:
            return existing
        job = repository.create(IndexJob(
            job_type="relation_vector_rebuild",
            status="pending",
            requested_by=requested_by,
            total_items=1,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
        ))
        queue = task_queue or self.container.task_queue
        queue.enqueue(job.id)
        return job

    def _relation_document(self, relation, subject, obj, quotes: list[str]):
        from langchain_core.documents import Document

        return Document(
            page_content="\n".join(filter(None, [
                f"{subject.canonical_name} --{relation.predicate}--> {obj.canonical_name}",
                f"关系：{'、'.join(relation_terms(relation.predicate))}",
                relation_description(relation.predicate),
                relation.description,
                *quotes,
            ])),
            metadata={
                "relation_id": relation.id,
                "knowledge_base_id": self.knowledge_base_id,
                "subject_id": subject.id,
                "subject_name": subject.canonical_name,
                "subject_type": subject.entity_type,
                "predicate": relation.predicate,
                "object_id": obj.id,
                "object_name": obj.canonical_name,
                "object_type": obj.entity_type,
                "confidence": float(relation.confidence),
            },
        )

    def apply_changes(
        self,
        changed_relation_ids: list[str] | None,
        deleted_relation_ids: list[str] | None,
    ) -> dict:
        """Incrementally upsert/delete relation vectors with full-rebuild fallback."""
        changed = list(dict.fromkeys(changed_relation_ids or []))
        deleted = list(dict.fromkeys(deleted_relation_ids or []))
        if not changed and not deleted:
            state = self.state()
            if state is not None and state.status == "ready":
                return {
                    "status": "unchanged",
                    "relation_count": state.relation_count,
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
                    ids=[f"relation:{relation_id}" for relation_id in deleted],
                )
            evidence_by_relation = defaultdict(list)
            for evidence in repository.list_relation_evidence(changed):
                evidence_by_relation[evidence.relation_id].append(evidence)
            documents = []
            ids = []
            stale: list[str] = []
            for relation_id in changed:
                if relation_id in deleted_set:
                    continue
                relation = self.db.get(KgRelation, relation_id)
                if relation is None:
                    stale.append(relation_id)
                    continue
                subject = repository.get_entity(relation.subject_entity_id)
                obj = repository.get_entity(relation.object_entity_id)
                if subject is None or obj is None:
                    stale.append(relation_id)
                    continue
                quotes = [
                    item.quote
                    for item in evidence_by_relation[relation_id][:3]
                ]
                documents.append(self._relation_document(relation, subject, obj, quotes))
                ids.append(f"relation:{relation.id}")
            if stale:
                self.container.vector_store.delete_vectors(
                    index_ref, ids=[f"relation:{item}" for item in stale]
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
            state.relation_count = repository.count_relations()
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
            "relation_count": state.relation_count,
            "index_name": state.index_name,
        }

    def rebuild(self) -> dict:
        repository = KnowledgeGraphRepository(self.db, self.knowledge_base_id)
        relations = repository.list_relations()
        entities = {entity.id: entity for entity in repository.list_entities()}
        evidence_by_relation = defaultdict(list)
        for evidence in repository.list_relation_evidence(
            [relation.id for relation in relations]
        ):
            evidence_by_relation[evidence.relation_id].append(evidence)

        embedding_identity = self.container.embedding.identity().as_dict()
        embedding_fingerprint = json.dumps(
            embedding_identity, sort_keys=True, ensure_ascii=False
        )
        fingerprint_rows = []
        for relation in relations:
            subject = entities.get(relation.subject_entity_id)
            obj = entities.get(relation.object_entity_id)
            fingerprint_rows.append(": ".join([
                relation.id,
                str(relation.updated_at),
                relation.predicate,
                subject.canonical_name if subject else "",
                str(subject.updated_at) if subject else "",
                obj.canonical_name if obj else "",
                str(obj.updated_at) if obj else "",
                ",".join(
                    item.quote_hash for item in evidence_by_relation[relation.id]
                ),
            ]))
        graph_fingerprint = hashlib.sha256(
            "\n".join(fingerprint_rows).encode("utf-8")
        ).hexdigest()

        state = self.state()
        if (
            state
            and state.status == "ready"
            and state.embedding_fingerprint == embedding_fingerprint
            and state.graph_fingerprint == graph_fingerprint
        ):
            return {
                "status": "unchanged",
                "relation_count": state.relation_count,
                "index_name": state.index_name,
            }

        old_index_name = state.index_name if state else None
        if state is None:
            state = RelationVectorIndexState(
                knowledge_base_id=self.knowledge_base_id
            )
            self.db.add(state)
        state.status = "building"
        self.db.commit()

        if not relations:
            state.index_name = None
            state.embedding_fingerprint = embedding_fingerprint
            state.graph_fingerprint = graph_fingerprint
            state.relation_count = 0
            state.status = "ready"
            self.db.commit()
            self._delete_index(old_index_name)
            return {"status": "ready", "relation_count": 0, "index_name": None}

        compact_kb = self.knowledge_base_id.replace("-", "")[:12]
        index_name = f"kg_relations_{compact_kb}_{graph_fingerprint[:12]}"
        index_ref = VectorIndexRef(
            name=index_name,
            knowledge_base_id=self.knowledge_base_id,
            index_version=graph_fingerprint[:12],
            embedding_profile_id=hashlib.sha256(
                embedding_fingerprint.encode("utf-8")
            ).hexdigest()[:12],
        )
        if index_name != old_index_name:
            self._delete_index(index_name)
        store = self.container.vector_store.open(
            self.container.embedding.get_model(), index_ref
        )
        documents = []
        ids = []
        for relation in relations:
            subject = entities.get(relation.subject_entity_id)
            obj = entities.get(relation.object_entity_id)
            if subject is None or obj is None:
                continue
            quotes = [
                item.quote for item in evidence_by_relation[relation.id][:3]
            ]
            documents.append(self._relation_document(relation, subject, obj, quotes))
            ids.append(f"relation:{relation.id}")

        try:
            for start in range(0, len(documents), INGEST_BATCH_SIZE):
                store.add_documents(
                    documents[start:start + INGEST_BATCH_SIZE],
                    ids=ids[start:start + INGEST_BATCH_SIZE],
                )
            state.index_name = index_name
            state.embedding_fingerprint = embedding_fingerprint
            state.graph_fingerprint = graph_fingerprint
            state.relation_count = len(documents)
            state.status = "ready"
            self.db.commit()
        except Exception:
            state.status = "failed"
            self.db.commit()
            raise
        if old_index_name and old_index_name != index_name:
            self._delete_index(old_index_name)
        return {
            "status": "ready",
            "relation_count": len(documents),
            "index_name": index_name,
        }

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
            VectorIndexRef(
                name=state.index_name,
                knowledge_base_id=self.knowledge_base_id,
            ),
        )
        try:
            if query_embedding is not None:
                rows = store.similarity_search_by_vector_with_relevance_scores(
                    query_embedding, k=limit
                )
            else:
                rows = store.similarity_search_with_relevance_scores(query, k=limit)
            return [
                {**document.metadata, "match_score": float(score)}
                for document, score in rows
            ]
        except (AttributeError, NotImplementedError):
            documents = (
                store.similarity_search_by_vector(query_embedding, k=limit)
                if query_embedding is not None
                else store.similarity_search(query, k=limit)
            )
            return [
                {**document.metadata, "match_score": 1.0 / (index + 1)}
                for index, document in enumerate(documents)
            ]

    def _delete_index(self, index_name: str | None) -> None:
        if not index_name:
            return
        try:
            self.container.vector_store.delete_index(VectorIndexRef(
                name=index_name,
                knowledge_base_id=self.knowledge_base_id,
            ))
        except Exception:
            pass
