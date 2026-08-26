"""Generate reviewable graph Schema drafts from indexed knowledge documents."""

from __future__ import annotations

import json
import re
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import (
    GRAPH_SCHEMA_SAMPLE_CHUNKS_PER_DOCUMENT,
    GRAPH_SCHEMA_SAMPLE_DOCUMENTS,
    GRAPH_SCHEMA_SAMPLE_MAX_CHARS,
)
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument


SCHEMA_INDUCTION_PROMPT = """你是企业知识图谱 Schema 设计师。请仅依据给定文档样本，归纳一个可用于后续实体关系抽取的 Schema 草稿。

要求：
1. 实体类型与关系类型使用简短、稳定的英文大写蛇形命名（例如 BUSINESS_PROCESS、DEPENDS_ON）。
2. 合并同义类型，避免按单个文档标题或具体实体创建类型。
3. 实体类型 3-30 个，关系类型 3-40 个；只保留样本中有证据支持的领域概念。
4. extraction_prompt 用中文说明领域边界、关键定义、歧义和禁止推断事项。
5. 只输出 JSON，不要 Markdown。格式：
{{"entity_types":["..."],"relation_types":["..."],"extraction_prompt":"...","rationale":"..."}}

用户补充要求：{instructions}

文档样本：
{samples}
"""


def _clean_json(raw: str) -> dict:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("Schema 生成结果必须是 JSON 对象")
    return payload


def _type_names(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    names: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("name") or value.get("type")
        if not isinstance(value, str):
            continue
        normalized = re.sub(r"[^A-Z0-9]+", "_", value.strip().upper()).strip("_")
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", normalized):
            names.append(normalized)
    return sorted(set(names))


class GraphSchemaInductionService:
    def __init__(self, db: Session, knowledge_base_id: str, *, llm=None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        if llm is None:
            from app.bootstrap.container import get_provider_container

            llm = get_provider_container().extraction_llm
        self.llm = llm

    def _documents(self, document_ids: list[str] | None) -> list[KnowledgeDocument]:
        stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
            KnowledgeDocument.status == "ready",
        )
        if document_ids:
            unique_ids = list(dict.fromkeys(document_ids))
            stmt = stmt.where(KnowledgeDocument.id.in_(unique_ids))
        documents = list(
            self.db.scalars(
                stmt.order_by(KnowledgeDocument.updated_at.desc()).limit(
                    GRAPH_SCHEMA_SAMPLE_DOCUMENTS
                )
            )
        )
        if not documents:
            raise ValueError("没有可用于生成 Schema 的已解析文档")
        return documents

    def _sample_chunks(self, documents: list[KnowledgeDocument]) -> list[KnowledgeChunk]:
        document_ids = [document.id for document in documents]
        chunks = list(
            self.db.scalars(
                select(KnowledgeChunk)
                .where(
                    KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                    KnowledgeChunk.document_id.in_(document_ids),
                )
                .order_by(KnowledgeChunk.document_id, KnowledgeChunk.chunk_index)
            )
        )
        grouped: dict[str, list[KnowledgeChunk]] = defaultdict(list)
        for chunk in chunks:
            grouped[chunk.document_id].append(chunk)

        sampled: list[KnowledgeChunk] = []
        per_document = GRAPH_SCHEMA_SAMPLE_CHUNKS_PER_DOCUMENT
        for document in documents:
            candidates = grouped.get(document.id, [])
            if len(candidates) <= per_document:
                sampled.extend(candidates)
                continue
            positions = {
                round(index * (len(candidates) - 1) / (per_document - 1))
                if per_document > 1
                else 0
                for index in range(per_document)
            }
            sampled.extend(candidates[position] for position in sorted(positions))
        if not sampled:
            raise ValueError("已解析文档尚未生成可采样的 Chunk")
        return sampled

    def _induce(
        self,
        *,
        document_ids: list[str] | None = None,
        instructions: str | None = None,
    ) -> dict:
        documents = self._documents(document_ids)
        chunks = self._sample_chunks(documents)
        names = {document.id: document.original_filename for document in documents}
        sample_blocks: list[str] = []
        used_chars = 0
        used_chunks = 0
        for chunk in chunks:
            block = (
                f"\n[文档: {names.get(chunk.document_id, chunk.document_id)}; "
                f"章节: {chunk.section or '-'}]\n{(chunk.content or '').strip()[:2500]}"
            )
            if used_chars + len(block) > GRAPH_SCHEMA_SAMPLE_MAX_CHARS:
                break
            sample_blocks.append(block)
            used_chars += len(block)
            used_chunks += 1
        if not sample_blocks:
            raise ValueError("文档样本为空，无法生成 Schema")

        raw = self.llm.generate(
            SCHEMA_INDUCTION_PROMPT.format(
                instructions=(instructions or "无").strip()[:2000],
                samples="\n".join(sample_blocks),
            )
        )
        payload = _clean_json(raw)
        entity_types = _type_names(payload.get("entity_types"))
        relation_types = _type_names(payload.get("relation_types"))
        extraction_prompt = str(payload.get("extraction_prompt") or "").strip()[:10000]
        rationale = str(payload.get("rationale") or "").strip()[:4000]

        return {
            "entity_types": entity_types,
            "relation_types": relation_types,
            "extraction_prompt": extraction_prompt or None,
            "source_document_ids": [document.id for document in documents],
            "generation_summary": {
                "source_documents": len(documents),
                "sampled_chunks": used_chunks,
                "sampled_characters": used_chars,
                "rationale": rationale,
            },
        }

    def _persist_draft(
        self,
        induced: dict,
        *,
        created_by: str | None,
        target_schema_id: str | None = None,
        generation_method: str = "document_llm",
    ):
        from app.services.graph_governance_service import GraphGovernanceService

        governance = GraphGovernanceService(
            self.db, self.knowledge_base_id, task_queue=object()
        )
        normalization_rules = {
            "unicode": "NFKC",
            "case": "lower_ascii",
            "trim_punctuation": True,
        }
        if target_schema_id:
            schema = governance.update_schema(
                target_schema_id,
                entity_types=induced["entity_types"],
                relation_types=induced["relation_types"],
                normalization_rules=normalization_rules,
                extraction_prompt=induced["extraction_prompt"],
            )
            if schema is None:
                raise ValueError("要覆盖的 Schema 草稿不存在")
            schema.generation_method = generation_method
            schema.source_document_ids_json = induced["source_document_ids"]
            schema.generation_summary_json = induced["generation_summary"]
            self.db.commit()
            self.db.refresh(schema)
            return schema
        return governance.create_schema(
            entity_types=induced["entity_types"],
            relation_types=induced["relation_types"],
            normalization_rules=normalization_rules,
            extraction_prompt=induced["extraction_prompt"],
            created_by=created_by,
            generation_method=generation_method,
            source_document_ids=induced["source_document_ids"],
            generation_summary=induced["generation_summary"],
        )

    def generate_draft(
        self,
        *,
        created_by: str | None,
        document_ids: list[str] | None = None,
        instructions: str | None = None,
        target_schema_id: str | None = None,
    ):
        induced = self._induce(
            document_ids=document_ids,
            instructions=instructions,
        )
        return self._persist_draft(
            induced,
            created_by=created_by,
            target_schema_id=target_schema_id,
        )

    def detect_drift(
        self,
        *,
        created_by: str | None,
        document_ids: list[str] | None = None,
        instructions: str | None = None,
    ) -> dict:
        """Compare document-induced concepts to the active schema.

        A reviewable draft is created only when at least one new entity or
        relation type appears. Existing active types are retained in the
        suggested draft so drift detection cannot accidentally narrow a domain.
        """
        from app.services.graph_governance_service import GraphGovernanceService

        governance = GraphGovernanceService(
            self.db, self.knowledge_base_id, task_queue=object()
        )
        active = governance.active_schema()
        induced = self._induce(
            document_ids=document_ids,
            instructions=instructions,
        )
        added_entities = sorted(
            set(induced["entity_types"]) - set(active.entity_types_json or [])
        )
        added_relations = sorted(
            set(induced["relation_types"]) - set(active.relation_types_json or [])
        )
        result = {
            "status": "no_drift",
            "active_schema_id": active.id,
            "active_schema_version": active.version,
            "added_entity_types": added_entities,
            "added_relation_types": added_relations,
            "source_document_ids": induced["source_document_ids"],
        }
        if not added_entities and not added_relations:
            return result

        induced["entity_types"] = sorted(
            set(active.entity_types_json or []) | set(added_entities)
        )
        induced["relation_types"] = sorted(
            set(active.relation_types_json or []) | set(added_relations)
        )
        summary = dict(induced["generation_summary"])
        summary.update({
            "drift_from_schema_id": active.id,
            "drift_from_schema_version": active.version,
            "added_entity_types": added_entities,
            "added_relation_types": added_relations,
        })
        induced["generation_summary"] = summary
        draft = self._persist_draft(
            induced,
            created_by=created_by,
            generation_method="document_drift",
        )
        result.update({
            "status": "drift_detected",
            "generated_schema_id": draft.id,
            "generated_schema_version": draft.version,
        })
        return result
