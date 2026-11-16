"""SQLite 图谱存储实现。"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from app.models.knowledge_graph import (
    KgAlias,
    KgEntity,
    KgEntityMention,
    KgGraphCorrection,
    KgRelation,
    KgRelationEvidence,
)
from app.models.graph_governance import GraphSchemaDefinition, KgEntityMerge
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from rag.knowledge_graph.normalizer import normalize_alias, normalize_name
from rag.knowledge_graph.relation_vocabulary import relation_query_scores
from rag.knowledge_graph.schemas import ChunkExtraction

logger = logging.getLogger(__name__)


def _text_match_score(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0
    if candidate in query:
        coverage = len(candidate) / max(len(query), 1)
        return 0.8 + 0.2 * coverage
    if query in candidate:
        coverage = len(query) / max(len(candidate), 1)
        return 0.5 + 0.3 * coverage
    return 0.0


class SqliteGraphStore:
    """基于 SQLAlchemy Session 的知识图谱存储。"""

    def __init__(self, db: Session, knowledge_base_id: str | None = None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id or DEFAULT_KNOWLEDGE_BASE_ID

    def _active_corrections(self) -> list[KgGraphCorrection]:
        return list(self.db.scalars(
            select(KgGraphCorrection).where(
                KgGraphCorrection.knowledge_base_id == self.knowledge_base_id,
                KgGraphCorrection.status == "active",
            )
        ))

    def _entity_overrides(self) -> dict[str, dict]:
        overrides: dict[str, dict] = {}
        for correction in self._active_corrections():
            if correction.action == "update_entity" and correction.target_id:
                overrides.setdefault(correction.target_id, {}).update(
                    correction.payload_json or {}
                )
        return overrides

    def _merge_map(self, *, check_cancelled=None) -> dict[str, str]:
        direct = {
            item.merged_entity_id: item.canonical_entity_id
            for item in self.db.scalars(select(KgEntityMerge).where(
                KgEntityMerge.knowledge_base_id == self.knowledge_base_id,
                KgEntityMerge.status == "active",
            ))
        }
        resolved: dict[str, str] = {}
        for source in direct:
            if check_cancelled:
                check_cancelled()
            current = source
            seen = {source}
            while current in direct and direct[current] not in seen:
                if check_cancelled and len(seen) % 32 == 0:
                    check_cancelled()
                current = direct[current]
                seen.add(current)
            resolved[source] = current
        return resolved

    def _relation_vocabulary(self) -> dict[str, dict[str, object]]:
        """Combine built-in aliases with the active Schema's domain vocabulary."""
        from rag.knowledge_graph.relation_vocabulary import RELATION_VOCABULARY

        vocabulary = {
            key: {**value} for key, value in RELATION_VOCABULARY.items()
        }
        schema = self.db.scalar(select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
            GraphSchemaDefinition.status == "active",
        ).order_by(GraphSchemaDefinition.version.desc()).limit(1))
        if schema is None:
            return vocabulary
        details = (schema.generation_summary_json or {}).get("relation_type_details") or []
        detail_by_name = {
            str(item.get("name", "")).upper(): item
            for item in details if isinstance(item, dict)
        }
        for predicate in schema.relation_types_json or []:
            name = str(predicate).upper()
            detail = detail_by_name.get(name, {})
            existing = vocabulary.get(name, {})
            aliases = [
                *existing.get("aliases", ()),
                *(detail.get("aliases") or []),
                detail.get("label", ""),
            ]
            vocabulary[name] = {
                "label": detail.get("label") or existing.get("label") or name.replace("_", " "),
                "description": detail.get("description") or existing.get("description") or "",
                "aliases": tuple(dict.fromkeys(str(item) for item in aliases if item)),
            }
        return vocabulary

    def get_effective_entities(self, entity_ids, *, check_cancelled=None) -> list[dict]:
        """Hydrate bounded semantic candidates and members merged into them."""
        requested = set(entity_ids)
        results = self.list_effective_entities(entity_ids=requested, check_cancelled=check_cancelled)
        merge_map = self._merge_map(check_cancelled=check_cancelled)
        for item in results:
            item["matched_entity_ids"] = sorted(identifier for identifier in requested
                                                if merge_map.get(identifier, identifier) == item["id"])
        return results

    def list_effective_entities(self, *, check_cancelled=None, entity_ids=None) -> list[dict]:
        """Return canonical entities after corrections and entity merges."""
        merge_map = self._merge_map(check_cancelled=check_cancelled)
        target_ids = None
        if entity_ids is not None:
            targets = {merge_map.get(str(item), str(item)) for item in entity_ids}
            target_ids = targets | {member for member, target in merge_map.items() if target in targets}
            if not target_ids:
                return []
        entity_query = select(KgEntity).where(
            KgEntity.knowledge_base_id == self.knowledge_base_id
        )
        alias_query = select(KgAlias).where(KgAlias.knowledge_base_id == self.knowledge_base_id)
        if target_ids is not None:
            entity_query = entity_query.where(KgEntity.id.in_(target_ids))
            alias_query = alias_query.where(KgAlias.entity_id.in_(target_ids))
        entities = list(self.db.scalars(entity_query.order_by(KgEntity.id)))
        aliases_by_entity: dict[str, list[str]] = {}
        for alias in self.db.scalars(alias_query):
            aliases_by_entity.setdefault(alias.entity_id, []).append(alias.alias)
        overrides = self._entity_overrides()
        entities_by_id = {entity.id: entity for entity in entities}
        grouped: dict[str, list[KgEntity]] = {}
        for entity in entities:
            grouped.setdefault(merge_map.get(entity.id, entity.id), []).append(entity)
        result: list[dict] = []
        for canonical_id, group in grouped.items():
            if check_cancelled:
                check_cancelled()
            canonical = entities_by_id.get(canonical_id)
            if canonical is None:
                continue
            override = overrides.get(canonical.id, {})
            aliases: list[str] = []
            for member in group:
                aliases.extend(aliases_by_entity.get(member.id, []))
                if member.id != canonical.id:
                    aliases.append(overrides.get(member.id, {}).get(
                        "canonical_name", member.canonical_name
                    ))
            canonical_name = override.get("canonical_name", canonical.canonical_name)
            result.append({
                "id": canonical.id,
                "entity_type": canonical.entity_type,
                "canonical_name": canonical_name,
                "normalized_name": normalize_name(canonical_name),
                "description": override.get("description", canonical.description),
                "aliases": list(dict.fromkeys(
                    item for item in aliases if item and item != canonical_name
                )),
                "updated_at": str(canonical.updated_at),
            })
        return result

    def list_effective_relations(self, *, check_cancelled=None) -> list[dict]:
        base_ids = list(self.db.scalars(select(KgRelation.id).where(
            KgRelation.knowledge_base_id == self.knowledge_base_id
        ).order_by(KgRelation.id)))
        manual_ids = [
            f"manual:{item.id}" for item in self._active_corrections()
            if item.action == "add_relation"
        ]
        return self.get_relations([*base_ids, *manual_ids], check_cancelled=check_cancelled)

    def upsert_extraction(
        self,
        chunk: dict,
        extraction: ChunkExtraction,
    ) -> None:
        """保存一个 chunk 的抽取结果。

        chunk: {"chunk_id":..., "document_id":..., "content":...}
        extraction: 校验后的 ChunkExtraction
        """
        chunk_id = extraction.chunk_id
        document_id = chunk["document_id"]

        # First delete existing data for this chunk
        self._delete_chunk_graph(chunk_id)

        local_id_to_entity_id: dict[str, str] = {}

        # Upsert entities
        for ent in extraction.entities:
            norm = normalize_name(ent.name)
            if not norm:
                continue

            existing = self.db.scalar(
                select(KgEntity).where(
                    KgEntity.knowledge_base_id == self.knowledge_base_id,
                    KgEntity.entity_type == ent.type,
                    KgEntity.normalized_name == norm,
                )
            )

            # A later chunk may use an abbreviation that an earlier extraction
            # already registered as an alias. Reuse that entity only when the
            # alias resolves uniquely inside the same type and knowledge base;
            # ambiguous aliases remain separate and enter the review workflow.
            if existing is None:
                alias_matches = list(self.db.scalars(
                    select(KgEntity)
                    .join(KgAlias, KgAlias.entity_id == KgEntity.id)
                    .where(
                        KgEntity.knowledge_base_id == self.knowledge_base_id,
                        KgEntity.entity_type == ent.type,
                        KgAlias.knowledge_base_id == self.knowledge_base_id,
                        KgAlias.normalized_alias == norm,
                    )
                    .distinct()
                    .limit(2)
                ))
                if len(alias_matches) == 1:
                    existing = alias_matches[0]

            if existing:
                entity = existing
                if ent.description and not entity.description:
                    entity.description = ent.description
            else:
                entity = KgEntity(
                    id=str(uuid.uuid4()),
                    knowledge_base_id=self.knowledge_base_id,
                    entity_type=ent.type,
                    canonical_name=ent.name,
                    normalized_name=norm,
                    description=ent.description,
                )
                self.db.add(entity)

            self.db.flush()
            local_id_to_entity_id[ent.local_id] = entity.id

            # Upsert aliases
            for alias in ent.aliases:
                norm_alias = normalize_alias(alias)
                if not norm_alias:
                    continue
                exists = self.db.scalar(
                    select(KgAlias).where(
                        KgAlias.knowledge_base_id == self.knowledge_base_id,
                        KgAlias.entity_id == entity.id,
                        KgAlias.normalized_alias == norm_alias,
                    )
                )
                if not exists:
                    self.db.add(KgAlias(
                        id=str(uuid.uuid4()),
                        knowledge_base_id=self.knowledge_base_id,
                        entity_id=entity.id,
                        alias=alias,
                        normalized_alias=norm_alias,
                    ))

            # Upsert mention
            mention_exists = self.db.scalar(
                select(KgEntityMention).where(
                    KgEntityMention.knowledge_base_id == self.knowledge_base_id,
                    KgEntityMention.entity_id == entity.id,
                    KgEntityMention.chunk_id == chunk_id,
                )
            )
            if not mention_exists:
                self.db.add(KgEntityMention(
                    id=str(uuid.uuid4()),
                    knowledge_base_id=self.knowledge_base_id,
                    entity_id=entity.id,
                    document_id=document_id,
                    chunk_id=chunk_id,
                    mention=ent.name,
                    confidence=1.0,
                ))

        # Upsert relations
        for rel in extraction.relations:
            subj_id = local_id_to_entity_id.get(rel.subject)
            obj_id = local_id_to_entity_id.get(rel.object)
            if not subj_id or not obj_id or subj_id == obj_id:
                continue

            existing = self.db.scalar(
                select(KgRelation).where(
                    KgRelation.knowledge_base_id == self.knowledge_base_id,
                    KgRelation.subject_entity_id == subj_id,
                    KgRelation.predicate == rel.predicate,
                    KgRelation.object_entity_id == obj_id,
                )
            )

            if existing:
                relation = existing
                if rel.confidence > relation.confidence:
                    relation.confidence = rel.confidence
            else:
                relation = KgRelation(
                    id=str(uuid.uuid4()),
                    knowledge_base_id=self.knowledge_base_id,
                    subject_entity_id=subj_id,
                    predicate=rel.predicate,
                    object_entity_id=obj_id,
                    confidence=rel.confidence,
                )
                self.db.add(relation)

            self.db.flush()

            # Upsert evidence
            quote_hash = hashlib.sha256(rel.evidence.encode("utf-8")).hexdigest()[:64]
            ev_exists = self.db.scalar(
                select(KgRelationEvidence).where(
                    KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
                    KgRelationEvidence.relation_id == relation.id,
                    KgRelationEvidence.chunk_id == chunk_id,
                    KgRelationEvidence.quote_hash == quote_hash,
                )
            )
            if not ev_exists:
                self.db.add(KgRelationEvidence(
                    id=str(uuid.uuid4()),
                    knowledge_base_id=self.knowledge_base_id,
                    relation_id=relation.id,
                    document_id=document_id,
                    chunk_id=chunk_id,
                    quote=rel.evidence,
                    quote_hash=quote_hash,
                    confidence=rel.confidence,
                ))

        self.db.commit()

    def _delete_chunk_graph(self, chunk_id: str) -> None:
        """删除 chunk 关联的提及和证据。"""
        self.db.execute(
            delete(KgEntityMention).where(
                KgEntityMention.knowledge_base_id == self.knowledge_base_id,
                KgEntityMention.chunk_id == chunk_id,
            )
        )
        self.db.execute(
            delete(KgRelationEvidence).where(
                KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
                KgRelationEvidence.chunk_id == chunk_id,
            )
        )
        self.db.commit()

    def delete_document_graph(self, document_id: str) -> None:
        """删除文档关联的全部图谱数据。"""
        self.db.execute(
            delete(KgEntityMention).where(
                KgEntityMention.knowledge_base_id == self.knowledge_base_id,
                KgEntityMention.document_id == document_id,
            )
        )
        self.db.execute(
            delete(KgRelationEvidence).where(
                KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
                KgRelationEvidence.document_id == document_id,
            )
        )
        self.db.commit()
        self.cleanup_orphans()

    def search_entities(self, query: str, limit: int = 20) -> list[dict]:
        """匹配实体名或别名，支持“问题中出现实体名”。

        例如实体“任务调度模块”可以命中“任务调度模块依赖什么”。
        """
        norm = normalize_name(query)
        if not norm:
            return []

        candidate_limit = max(limit * 4, limit)
        entities = list(self.db.scalars(
            select(KgEntity).where(
                KgEntity.knowledge_base_id == self.knowledge_base_id,
                or_(
                    KgEntity.normalized_name == norm,
                    KgEntity.normalized_name.contains(norm),
                    literal(norm).contains(KgEntity.normalized_name),
                )
            ).limit(candidate_limit)
        ))
        alias_matches = list(self.db.scalars(
            select(KgAlias).where(
                KgAlias.knowledge_base_id == self.knowledge_base_id,
                or_(
                    KgAlias.normalized_alias == norm,
                    KgAlias.normalized_alias.contains(norm),
                    literal(norm).contains(KgAlias.normalized_alias),
                )
            ).limit(candidate_limit)
        ))

        entities_by_id = {entity.id: entity for entity in entities}
        for alias in alias_matches:
            if alias.entity_id not in entities_by_id:
                entity = self.db.get(KgEntity, alias.entity_id)
                if entity is not None:
                    entities_by_id[entity.id] = entity

        entity_overrides = self._entity_overrides()
        override_scores: dict[str, float] = {}
        for entity_id, payload in entity_overrides.items():
            corrected_name = normalize_name(str(payload.get("canonical_name", "")))
            override_scores[entity_id] = _text_match_score(norm, corrected_name)
            if corrected_name and _text_match_score(norm, corrected_name) > 0:
                entity = self.db.get(KgEntity, entity_id)
                if entity is not None and entity.knowledge_base_id == self.knowledge_base_id:
                    entities_by_id[entity.id] = entity

        alias_scores: dict[str, float] = {}
        for alias in alias_matches:
            alias_scores[alias.entity_id] = max(
                alias_scores.get(alias.entity_id, 0.0),
                _text_match_score(norm, alias.normalized_alias) - 0.25,
            )

        ranked_entities = sorted(
            entities_by_id.values(),
            key=lambda entity: (
                -max(
                    _text_match_score(norm, entity.normalized_name),
                    alias_scores.get(entity.id, 0.0),
                    override_scores.get(entity.id, 0.0),
                ),
                len(entity.normalized_name),
                entity.canonical_name,
            ),
        )[:limit]

        result = []
        merge_map = self._merge_map()
        returned: set[str] = set()
        for ent in ranked_entities:
            canonical_id = merge_map.get(ent.id, ent.id)
            if canonical_id in returned:
                continue
            if canonical_id != ent.id:
                canonical = self.db.get(KgEntity, canonical_id)
                if canonical is None:
                    continue
                ent = canonical
            returned.add(ent.id)
            override = entity_overrides.get(ent.id, {})
            aliases = list(self.db.scalars(
                select(KgAlias).where(
                    KgAlias.knowledge_base_id == self.knowledge_base_id,
                    KgAlias.entity_id == ent.id,
                )
            ))
            match_score = max(
                _text_match_score(norm, ent.normalized_name),
                alias_scores.get(ent.id, 0.0),
                override_scores.get(ent.id, 0.0),
            )
            result.append({
                "id": ent.id,
                "entity_type": ent.entity_type,
                "canonical_name": override.get("canonical_name", ent.canonical_name),
                "normalized_name": normalize_name(
                    override.get("canonical_name", ent.canonical_name)
                ),
                "description": override.get("description", ent.description),
                "aliases": [a.alias for a in aliases],
                "match_score": match_score,
            })
        return result

    def get_neighbors(
        self,
        entity_ids: list[str],
        max_hops: int = 2,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """获取实体邻居关系（稳定顺序 BFS，分页下沉到 Store）。"""
        if not entity_ids:
            return []

        offset = max(0, offset)
        fetch_limit = offset + max(0, limit)
        if fetch_limit == 0:
            return []

        visited: set[str] = set()
        current = set(entity_ids)
        relations_result: list[dict] = []
        count = 0

        for hop in range(1, max_hops + 1):
            if not current or count >= fetch_limit:
                break

            next_layer: set[str] = set()

            blocked = list(visited | current)
            relations = self.db.scalars(
                select(KgRelation).where(
                    KgRelation.knowledge_base_id == self.knowledge_base_id,
                    or_(
                        (
                            KgRelation.subject_entity_id.in_(list(current))
                            & KgRelation.object_entity_id.notin_(blocked)
                        ),
                        (
                            KgRelation.object_entity_id.in_(list(current))
                            & KgRelation.subject_entity_id.notin_(blocked)
                        ),
                    ),
                )
                .order_by(KgRelation.confidence.desc(), KgRelation.id)
                .limit(fetch_limit - count)
            ).all()

            for rel in relations:
                if count >= fetch_limit:
                    break
                subj = self.db.get(KgEntity, rel.subject_entity_id)
                obj = self.db.get(KgEntity, rel.object_entity_id)
                if not subj or not obj:
                    continue

                relations_result.append({
                    "relation_id": rel.id,
                    "hop": hop,
                    "subject_id": subj.id,
                    "subject_name": subj.canonical_name,
                    "subject_type": subj.entity_type,
                    "predicate": rel.predicate,
                    "object_id": obj.id,
                    "object_name": obj.canonical_name,
                    "object_type": obj.entity_type,
                    "confidence": rel.confidence,
                })

                next_layer.add(rel.subject_entity_id)
                next_layer.add(rel.object_entity_id)
                count += 1

            visited.update(current)
            current = next_layer - visited

        corrections = self._active_corrections()
        hidden = {
            correction.target_id
            for correction in corrections
            if correction.action in {"hide_relation", "merge_relation"}
        }
        relation_updates = {
            correction.target_id: correction
            for correction in corrections
            if correction.action == "update_relation" and correction.target_id
        }
        entity_overrides = self._entity_overrides()
        overlaid: list[dict] = []
        relevant_entity_ids = set(entity_ids)
        for relation in relations_result:
            if relation["relation_id"] in hidden:
                continue
            updated = relation_updates.get(relation["relation_id"])
            item = dict(relation)
            if updated is not None:
                payload = updated.payload_json or {}
                item["predicate"] = payload.get("predicate", item["predicate"])
                item["confidence"] = float(payload.get("confidence", item["confidence"]))
                item["manual"] = True
                item["correction_id"] = updated.id
            subject_override = entity_overrides.get(item["subject_id"], {})
            object_override = entity_overrides.get(item["object_id"], {})
            item["subject_name"] = subject_override.get("canonical_name", item["subject_name"])
            item["object_name"] = object_override.get("canonical_name", item["object_name"])
            relevant_entity_ids.update((item["subject_id"], item["object_id"]))
            overlaid.append(item)

        for correction in corrections:
            if correction.action != "add_relation" or len(overlaid) >= fetch_limit:
                continue
            payload = correction.payload_json or {}
            subject_id = payload.get("subject_entity_id")
            object_id = payload.get("object_entity_id")
            if subject_id not in relevant_entity_ids and object_id not in relevant_entity_ids:
                continue
            relation_id = f"manual:{correction.id}"
            if relation_id in hidden:
                continue
            subject = self.db.get(KgEntity, subject_id)
            obj = self.db.get(KgEntity, object_id)
            if subject is None or obj is None:
                continue
            subject_override = entity_overrides.get(subject.id, {})
            object_override = entity_overrides.get(obj.id, {})
            updated = relation_updates.get(relation_id)
            update_payload = (updated.payload_json or {}) if updated else {}
            overlaid.append({
                "relation_id": relation_id,
                "hop": 1,
                "subject_id": subject.id,
                "subject_name": subject_override.get("canonical_name", subject.canonical_name),
                "subject_type": subject.entity_type,
                "predicate": update_payload.get("predicate", payload["predicate"]),
                "object_id": obj.id,
                "object_name": object_override.get("canonical_name", obj.canonical_name),
                "object_type": obj.entity_type,
                "confidence": float(update_payload.get("confidence", payload.get("confidence", 1.0))),
                "manual": True,
                "correction_id": updated.id if updated else correction.id,
            })
        merge_map = self._merge_map()
        merged_relations: dict[tuple[str, str, str], dict] = {}
        for relation in overlaid:
            item = dict(relation)
            item["subject_id"] = merge_map.get(item["subject_id"], item["subject_id"])
            item["object_id"] = merge_map.get(item["object_id"], item["object_id"])
            if item["subject_id"] == item["object_id"]:
                continue
            subject = self.db.get(KgEntity, item["subject_id"])
            obj = self.db.get(KgEntity, item["object_id"])
            if subject:
                item["subject_name"] = entity_overrides.get(subject.id, {}).get(
                    "canonical_name", subject.canonical_name
                )
                item["subject_type"] = subject.entity_type
            if obj:
                item["object_name"] = entity_overrides.get(obj.id, {}).get(
                    "canonical_name", obj.canonical_name
                )
                item["object_type"] = obj.entity_type
            key = (item["subject_id"], item["predicate"], item["object_id"])
            previous = merged_relations.get(key)
            if previous is None or item["confidence"] > previous["confidence"]:
                merged_relations[key] = item
        return list(merged_relations.values())[offset:offset + limit]

    def search_relations(self, query: str, limit: int = 20) -> list[dict]:
        """Match relation predicates using canonical names and multilingual aliases."""
        scores = relation_query_scores(query, self._relation_vocabulary())
        if not scores or limit <= 0:
            return []

        corrections = self._active_corrections()
        updated_ids = []
        manual_ids = []
        for correction in corrections:
            predicate = str((correction.payload_json or {}).get("predicate", ""))
            if predicate not in scores:
                continue
            if correction.action == "update_relation" and correction.target_id:
                updated_ids.append(correction.target_id)
            elif correction.action == "add_relation":
                manual_ids.append(f"manual:{correction.id}")

        candidate_limit = max(limit * 4, limit)
        base_ids = list(self.db.scalars(
            select(KgRelation.id)
            .where(
                KgRelation.knowledge_base_id == self.knowledge_base_id,
                KgRelation.predicate.in_(list(scores)),
            )
            .order_by(KgRelation.confidence.desc(), KgRelation.id)
            .limit(candidate_limit)
        ))
        relation_ids = list(dict.fromkeys([*base_ids, *updated_ids, *manual_ids]))
        matches = []
        for relation in self.get_relations(relation_ids):
            match_score = scores.get(relation["predicate"], 0.0)
            if match_score <= 0:
                continue
            matches.append({
                **relation,
                "match_score": match_score,
                "match_method": "relation_lexical",
            })
        return sorted(
            matches,
            key=lambda item: (
                -float(item["match_score"]),
                -float(item["confidence"]),
                item["relation_id"],
            ),
        )[:limit]

    def get_relations(self, relation_ids: list[str], *, check_cancelled=None) -> list[dict]:
        """Hydrate base/manual relations and apply active governance overlays."""
        if not relation_ids:
            return []

        base_ids = [item for item in relation_ids if not item.startswith("manual:")]
        manual_ids = {
            item.split(":", 1)[1]
            for item in relation_ids
            if item.startswith("manual:")
        }
        subject = aliased(KgEntity)
        obj = aliased(KgEntity)
        # Full-KB views may contain more identifiers than a database driver's
        # bind-parameter limit. Hydrate bounded batches, then apply governance
        # and deduplicate across the complete result, never per batch.
        rows = []
        for start in range(0, len(base_ids), 500):
            if check_cancelled:
                check_cancelled()
            rows.extend(self.db.execute(
                select(KgRelation, subject, obj)
                .join(subject, subject.id == KgRelation.subject_entity_id)
                .join(obj, obj.id == KgRelation.object_entity_id)
                .where(
                    KgRelation.knowledge_base_id == self.knowledge_base_id,
                    KgRelation.id.in_(base_ids[start:start + 500]),
                )
            ).all())

        corrections = self._active_corrections()
        hidden = {
            correction.target_id
            for correction in corrections
            if correction.action in {"hide_relation", "merge_relation"} and correction.target_id
        }
        relation_updates = {
            correction.target_id: correction
            for correction in corrections
            if correction.action == "update_relation" and correction.target_id
        }
        entity_overrides = self._entity_overrides()
        items: list[dict] = []
        for position, (relation, subject_entity, object_entity) in enumerate(rows):
            if check_cancelled and position % 32 == 0:
                check_cancelled()
            if relation.id in hidden:
                continue
            update = relation_updates.get(relation.id)
            payload = (update.payload_json or {}) if update else {}
            items.append({
                "relation_id": relation.id,
                "hop": 0,
                "subject_id": subject_entity.id,
                "subject_name": entity_overrides.get(subject_entity.id, {}).get(
                    "canonical_name", subject_entity.canonical_name
                ),
                "subject_type": subject_entity.entity_type,
                "predicate": payload.get("predicate", relation.predicate),
                "object_id": object_entity.id,
                "object_name": entity_overrides.get(object_entity.id, {}).get(
                    "canonical_name", object_entity.canonical_name
                ),
                "object_type": object_entity.entity_type,
                "confidence": float(payload.get("confidence", relation.confidence)),
                **({"manual": True, "correction_id": update.id} if update else {}),
            })

        if manual_ids:
            for correction in corrections:
                if check_cancelled:
                    check_cancelled()
                if correction.id not in manual_ids or correction.action != "add_relation":
                    continue
                payload = correction.payload_json or {}
                subject_entity = self.db.get(KgEntity, payload.get("subject_entity_id"))
                object_entity = self.db.get(KgEntity, payload.get("object_entity_id"))
                if subject_entity is None or object_entity is None:
                    continue
                relation_id = f"manual:{correction.id}"
                if relation_id in hidden:
                    continue
                update = relation_updates.get(relation_id)
                update_payload = (update.payload_json or {}) if update else {}
                items.append({
                    "relation_id": relation_id,
                    "hop": 0,
                    "subject_id": subject_entity.id,
                    "subject_name": entity_overrides.get(subject_entity.id, {}).get(
                        "canonical_name", subject_entity.canonical_name
                    ),
                    "subject_type": subject_entity.entity_type,
                    "predicate": update_payload.get("predicate", payload["predicate"]),
                    "object_id": object_entity.id,
                    "object_name": entity_overrides.get(object_entity.id, {}).get(
                        "canonical_name", object_entity.canonical_name
                    ),
                    "object_type": object_entity.entity_type,
                    "confidence": float(update_payload.get("confidence", payload.get("confidence", 1.0))),
                    "manual": True,
                    "correction_id": update.id if update else correction.id,
                })

        merge_map = self._merge_map(check_cancelled=check_cancelled)
        merged: dict[tuple[str, str, str], dict] = {}
        for position, relation in enumerate(items):
            if check_cancelled and position % 32 == 0:
                check_cancelled()
            item = dict(relation)
            item["subject_id"] = merge_map.get(item["subject_id"], item["subject_id"])
            item["object_id"] = merge_map.get(item["object_id"], item["object_id"])
            if item["subject_id"] == item["object_id"]:
                continue
            for prefix in ("subject", "object"):
                entity = self.db.get(KgEntity, item[f"{prefix}_id"])
                if entity is not None:
                    item[f"{prefix}_name"] = entity_overrides.get(entity.id, {}).get(
                        "canonical_name", entity.canonical_name
                    )
                    item[f"{prefix}_type"] = entity.entity_type
            key = (item["subject_id"], item["predicate"], item["object_id"])
            previous = merged.get(key)
            if previous is None or item["confidence"] > previous["confidence"]:
                merged[key] = item

        by_id = {item["relation_id"]: item for item in merged.values()}
        return [by_id[item] for item in relation_ids if item in by_id]

    def get_relation_evidence(self, relation_ids: list[str]) -> list[dict]:
        """获取关系的原文证据。"""
        if not relation_ids:
            return []
        base_relation_ids = [item for item in relation_ids if not item.startswith("manual:")]
        evidence_relation_map = {item: item for item in base_relation_ids}
        for correction in self._active_corrections():
            if correction.action != "merge_relation" or not correction.target_id:
                continue
            canonical_id = str((correction.payload_json or {}).get("canonical_relation_id", ""))
            if canonical_id in base_relation_ids:
                evidence_relation_map[correction.target_id] = canonical_id
        evidence_source_ids = list(evidence_relation_map)
        evidence = self.db.scalars(
            select(KgRelationEvidence).where(
                KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
                KgRelationEvidence.relation_id.in_(evidence_source_ids)
            )
        ).all() if evidence_source_ids else []
        result = [
            {
                "relation_id": evidence_relation_map.get(ev.relation_id, ev.relation_id),
                "document_id": ev.document_id,
                "chunk_id": ev.chunk_id,
                "quote": ev.quote,
                "confidence": ev.confidence,
            }
            for ev in evidence
        ]
        manual_ids = {
            item.split(":", 1)[1]
            for item in relation_ids
            if item.startswith("manual:")
        }
        if manual_ids:
            corrections = list(self.db.scalars(
                select(KgGraphCorrection).where(
                    KgGraphCorrection.knowledge_base_id == self.knowledge_base_id,
                    KgGraphCorrection.id.in_(manual_ids),
                    KgGraphCorrection.status == "active",
                )
            ))
            for correction in corrections:
                payload = correction.payload_json or {}
                result.append({
                    "relation_id": f"manual:{correction.id}",
                    "document_id": payload.get("document_id"),
                    "chunk_id": payload.get("chunk_id") or f"manual:{correction.id}",
                    "quote": payload.get("evidence", correction.reason),
                    "confidence": float(payload.get("confidence", 1.0)),
                })
        return result

    def get_entity_evidence(self, entity_ids: list[str]) -> list[dict]:
        """Map effective entity ids to real source chunks through mentions."""
        if not entity_ids:
            return []
        requested = set(entity_ids)
        merge_map = self._merge_map()
        source_ids = requested | {
            source for source, canonical in merge_map.items() if canonical in requested
        }
        mentions = list(self.db.scalars(select(KgEntityMention).where(
            KgEntityMention.knowledge_base_id == self.knowledge_base_id,
            KgEntityMention.entity_id.in_(source_ids),
        )))
        effective_by_id = {
            item["id"]: item for item in self.list_effective_entities()
        }
        result = []
        seen: set[tuple[str, str]] = set()
        for mention in mentions:
            entity_id = merge_map.get(mention.entity_id, mention.entity_id)
            entity = effective_by_id.get(entity_id)
            key = (entity_id, mention.chunk_id)
            if entity is None or key in seen:
                continue
            seen.add(key)
            result.append({
                "entity_id": entity_id,
                "entity_name": entity["canonical_name"],
                "entity_type": entity["entity_type"],
                "document_id": mention.document_id,
                "chunk_id": mention.chunk_id,
                "mention": mention.mention,
                "confidence": float(mention.confidence),
            })
        return result

    def get_document_subgraph(self, document_id: str, limit: int = 200) -> dict:
        """Return entities and relations supported by a document's evidence."""
        mentions = list(self.db.scalars(
            select(KgEntityMention).where(
                KgEntityMention.knowledge_base_id == self.knowledge_base_id,
                KgEntityMention.document_id == document_id,
            )
        ))
        # Rank relations rather than raw evidence rows. A heavily cited relation
        # must not consume the whole visual budget and hide every other fact.
        relation_ids = list(self.db.scalars(
            select(KgRelationEvidence.relation_id)
            .where(
                KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
                KgRelationEvidence.document_id == document_id,
            )
            .group_by(KgRelationEvidence.relation_id)
            .order_by(
                func.max(KgRelationEvidence.confidence).desc(),
                KgRelationEvidence.relation_id,
            )
            .limit(limit)
        ))
        evidence = list(self.db.scalars(
            select(KgRelationEvidence).where(
                KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
                KgRelationEvidence.document_id == document_id,
                KgRelationEvidence.relation_id.in_(relation_ids),
            )
        )) if relation_ids else []
        relations = []
        if relation_ids:
            relation_by_id = {
                relation.id: relation
                for relation in self.db.scalars(
                select(KgRelation).where(
                    KgRelation.knowledge_base_id == self.knowledge_base_id,
                    KgRelation.id.in_(relation_ids),
                )
                )
            }
            relations = [
                relation_by_id[relation_id]
                for relation_id in relation_ids
                if relation_id in relation_by_id
            ]

        entity_ids = {item.entity_id for item in mentions}
        for relation in relations:
            entity_ids.add(relation.subject_entity_id)
            entity_ids.add(relation.object_entity_id)
        corrections = self._active_corrections()
        manual_relations = [
            correction
            for correction in corrections
            if correction.action == "add_relation"
            and (correction.payload_json or {}).get("document_id") == document_id
        ]
        for correction in manual_relations:
            payload = correction.payload_json or {}
            entity_ids.update((payload.get("subject_entity_id"), payload.get("object_entity_id")))
        entity_ids.discard(None)
        entities = []
        if entity_ids:
            entities = list(self.db.scalars(
                select(KgEntity).where(
                    KgEntity.knowledge_base_id == self.knowledge_base_id,
                    KgEntity.id.in_(entity_ids),
                )
            ))

        mention_counts: dict[str, int] = {}
        for mention in mentions:
            mention_counts[mention.entity_id] = mention_counts.get(mention.entity_id, 0) + 1
        evidence_by_relation: dict[str, list[dict]] = {}
        for item in evidence:
            evidence_by_relation.setdefault(item.relation_id, []).append({
                "chunk_id": item.chunk_id,
                "quote": item.quote,
                "confidence": item.confidence,
            })

        hidden = {
            correction.target_id
            for correction in corrections
            if correction.action in {"hide_relation", "merge_relation"}
        }
        relation_updates = {
            correction.target_id: correction
            for correction in corrections
            if correction.action == "update_relation" and correction.target_id
        }
        entity_overrides = self._entity_overrides()
        nodes = []
        for entity in entities:
            override = entity_overrides.get(entity.id, {})
            nodes.append({
                "id": entity.id,
                "label": override.get("canonical_name", entity.canonical_name),
                "entity_type": entity.entity_type,
                "description": override.get("description", entity.description),
                "mention_count": mention_counts.get(entity.id, 0),
            })

        edges = []
        for relation in relations:
            if relation.id in hidden:
                continue
            updated = relation_updates.get(relation.id)
            payload = updated.payload_json if updated is not None else {}
            edges.append({
                "id": relation.id,
                "source": relation.subject_entity_id,
                "target": relation.object_entity_id,
                "predicate": payload.get("predicate", relation.predicate),
                "confidence": float(payload.get("confidence", relation.confidence)),
                "evidence": evidence_by_relation.get(relation.id, []),
                "manual": updated is not None,
                "correction_id": updated.id if updated is not None else None,
            })
        for correction in manual_relations:
            payload = correction.payload_json or {}
            relation_id = f"manual:{correction.id}"
            if relation_id in hidden:
                continue
            updated = relation_updates.get(relation_id)
            update_payload = (updated.payload_json or {}) if updated else {}
            edges.append({
                "id": relation_id,
                "source": payload["subject_entity_id"],
                "target": payload["object_entity_id"],
                "predicate": update_payload.get("predicate", payload["predicate"]),
                "confidence": float(update_payload.get("confidence", payload.get("confidence", 1.0))),
                "evidence": [{
                    "chunk_id": payload.get("chunk_id") or f"manual:{correction.id}",
                    "quote": payload.get("evidence", correction.reason),
                    "confidence": float(payload.get("confidence", 1.0)),
                }],
                "manual": True,
                "correction_id": updated.id if updated else correction.id,
            })

        return {
            "document_id": document_id,
            "nodes": nodes,
            "edges": edges[:limit],
        }

    def cleanup_orphans(self) -> None:
        """清理无提及、无关系的孤立实体和无证据关系。"""
        # Delete relations without evidence
        relations_with_evidence = self.db.scalars(
            select(KgRelationEvidence.relation_id)
            .where(KgRelationEvidence.knowledge_base_id == self.knowledge_base_id)
            .distinct()
        ).all()
        if relations_with_evidence:
            self.db.execute(
                delete(KgRelation).where(
                    KgRelation.knowledge_base_id == self.knowledge_base_id,
                    KgRelation.id.notin_(relations_with_evidence)
                )
            )
        else:
            self.db.execute(
                delete(KgRelation).where(
                    KgRelation.knowledge_base_id == self.knowledge_base_id
                )
            )

        # Delete entities without mentions and without relations
        entities_with_mentions = self.db.scalars(
            select(KgEntityMention.entity_id)
            .where(KgEntityMention.knowledge_base_id == self.knowledge_base_id)
            .distinct()
        ).all()
        entities_in_relations = set(
            list(self.db.scalars(
                select(KgRelation.subject_entity_id)
                .where(KgRelation.knowledge_base_id == self.knowledge_base_id)
                .distinct()
            )) +
            list(self.db.scalars(
                select(KgRelation.object_entity_id)
                .where(KgRelation.knowledge_base_id == self.knowledge_base_id)
                .distinct()
            ))
        )
        keep_ids = set(entities_with_mentions) | entities_in_relations

        all_entities = self.db.scalars(
            select(KgEntity).where(
                KgEntity.knowledge_base_id == self.knowledge_base_id
            )
        ).all()
        for ent in all_entities:
            if ent.id not in keep_ids:
                self.db.delete(ent)

        self.db.commit()

    def stats(self) -> dict:
        return {
            "entities": self.db.scalar(
                select(func.count(KgEntity.id)).where(
                    KgEntity.knowledge_base_id == self.knowledge_base_id
                )
            ) or 0,
            "aliases": self.db.scalar(
                select(func.count(KgAlias.id)).where(
                    KgAlias.knowledge_base_id == self.knowledge_base_id
                )
            ) or 0,
            "mentions": self.db.scalar(
                select(func.count(KgEntityMention.id)).where(
                    KgEntityMention.knowledge_base_id == self.knowledge_base_id
                )
            ) or 0,
            "relations": self.db.scalar(
                select(func.count(KgRelation.id)).where(
                    KgRelation.knowledge_base_id == self.knowledge_base_id
                )
            ) or 0,
            "evidence": self.db.scalar(
                select(func.count(KgRelationEvidence.id)).where(
                    KgRelationEvidence.knowledge_base_id == self.knowledge_base_id
                )
            ) or 0,
            "active_corrections": self.db.scalar(
                select(func.count(KgGraphCorrection.id)).where(
                    KgGraphCorrection.knowledge_base_id == self.knowledge_base_id,
                    KgGraphCorrection.status == "active",
                )
            ) or 0,
            "conflict_corrections": self.db.scalar(
                select(func.count(KgGraphCorrection.id)).where(
                    KgGraphCorrection.knowledge_base_id == self.knowledge_base_id,
                    KgGraphCorrection.status == "conflict",
                )
            ) or 0,
        }
