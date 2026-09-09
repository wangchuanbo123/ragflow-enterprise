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
from rag.knowledge_graph.profiles import get_graph_profile, profile_schema, profiles_for_schema


SCHEMA_INDUCTION_PROMPT = """你是企业知识图谱 Schema 设计师。请仅依据给定文档样本，归纳一个可用于后续实体关系抽取的 Schema 草稿。

要求：
1. 实体类型与关系类型使用简短、稳定的英文大写蛇形命名（例如 BUSINESS_PROCESS、DEPENDS_ON）。
2. 合并同义类型，避免按单个文档标题或具体实体创建类型；核心实体优先控制在 8-15 个、核心关系优先控制在 10-25 个。
3. 最多保留 30 个实体类型和 40 个关系类型，只保留样本中有证据支持且对检索有价值的领域概念。
4. 每个实体类型给出中文定义、常见别名、反例，并用 graph_kind 标注主要视图：concept、functional、configuration、standard 四选一。
5. 每个关系类型给出中文定义、允许的起点类型 source_types、终点类型 target_types、方向说明和 graph_kind；避免创建语义重复的正反谓词。
6. extraction_prompt 用中文说明领域边界、实体规范名规则、关系方向、歧义、反例和禁止推断事项。
7. 只输出 JSON，不要 Markdown。格式：
{{"entity_types":[{{"name":"...","description":"...","aliases":["..."],"exclusions":["..."],"graph_kind":"configuration"}}],"relation_types":[{{"name":"...","description":"...","source_types":["..."],"target_types":["..."],"direction":"...","graph_kind":"configuration"}}],"extraction_prompt":"...","rationale":"..."}}

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


def _type_names(values: object, *, limit: int) -> list[str]:
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
    return sorted(set(names))[:limit]


def _schema_details(
    values: object,
    allowed_names: set[str],
    *,
    relation: bool,
    endpoint_names: set[str] | None = None,
) -> list[dict]:
    """Keep compact, validated type guidance for prompts and provenance."""
    if not isinstance(values, list):
        return []
    details: list[dict] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        names = _type_names([value], limit=1)
        if not names or names[0] not in allowed_names:
            continue
        item = {
            "name": names[0],
            "description": str(value.get("description") or "").strip()[:300],
            "graph_kind": (
                str(value.get("graph_kind") or "").strip().lower()
                if str(value.get("graph_kind") or "").strip().lower()
                in {"concept", "functional", "configuration", "standard"}
                else "configuration"
            ),
        }
        if relation:
            allowed_endpoints = endpoint_names or set()
            item.update({
                "source_types": [
                    name for name in _type_names(value.get("source_types"), limit=12)
                    if name in allowed_endpoints
                ],
                "target_types": [
                    name for name in _type_names(value.get("target_types"), limit=12)
                    if name in allowed_endpoints
                ],
                "direction": str(value.get("direction") or "directed").strip()[:80],
            })
        else:
            item.update({
                "aliases": [str(item).strip()[:80] for item in value.get("aliases", []) if str(item).strip()][:10],
                "exclusions": [str(item).strip()[:120] for item in value.get("exclusions", []) if str(item).strip()][:8],
            })
        details.append(item)
    return details


def _enrich_extraction_prompt(base_prompt: str, entity_details: list[dict], relation_details: list[dict]) -> str:
    guidance: list[str] = []
    if entity_details:
        guidance.append("实体类型定义：")
        for item in entity_details:
            aliases = "、".join(item.get("aliases") or [])
            exclusions = "、".join(item.get("exclusions") or [])
            guidance.append(
                f"- {item['name']}：{item.get('description') or '按领域含义识别'}"
                + (f"；别名：{aliases}" if aliases else "")
                + (f"；不要抽取：{exclusions}" if exclusions else "")
            )
    if relation_details:
        guidance.append("关系方向与端点约束：")
        for item in relation_details:
            sources = "、".join(item.get("source_types") or []) or "Schema 内任意适用类型"
            targets = "、".join(item.get("target_types") or []) or "Schema 内任意适用类型"
            guidance.append(
                f"- {item['name']}：{item.get('description') or '按领域含义识别'}；"
                f"{sources} → {targets}；方向：{item.get('direction') or 'directed'}"
            )
    parts = [base_prompt.strip()] if base_prompt.strip() else []
    if guidance:
        parts.append("\n".join(guidance))
    return "\n\n".join(parts)[:10000]


class GraphSchemaInductionService:
    def __init__(
        self,
        db: Session,
        knowledge_base_id: str,
        *,
        llm=None,
        max_retries: int = 1,
    ):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        self.max_retries = max_retries
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

    def _sample_blocks(
        self,
        documents: list[KnowledgeDocument],
        chunks: list[KnowledgeChunk],
    ) -> list[str]:
        """Build per-document sample blocks under an equal-share char budget.

        每篇文档均摊总预算，保证全部采样文档都贡献样本：
        - 单篇超配额时先压缩 chunk 截断长度，仍超时按比例丢弃中间 chunk；
        - 某篇完全放不下时跳过该篇（而不是终止整体装配）。
        """
        names = {document.id: document.original_filename for document in documents}
        grouped: dict[str, list[KnowledgeChunk]] = defaultdict(list)
        for chunk in chunks:
            grouped[chunk.document_id].append(chunk)

        per_document_budget = GRAPH_SCHEMA_SAMPLE_MAX_CHARS // max(len(documents), 1)
        blocks: list[str] = []
        for document in documents:
            candidates = grouped.get(document.id, [])
            if not candidates:
                continue
            name = names.get(document.id, document.id)

            def _block(chunk: KnowledgeChunk, limit: int) -> str:
                return (
                    f"\n[文档: {name}; 章节: {chunk.section or '-'}]\n"
                    f"{(chunk.content or '').strip()[:limit]}"
                )

            head = len(_block(candidates[0], 0))
            chunk_count = len(candidates)
            content_budget = max(per_document_budget - head * chunk_count, 200)
            per_chunk_limit = content_budget // chunk_count

            if per_chunk_limit >= 200:
                blocks.extend(_block(c, per_chunk_limit) for c in candidates)
                continue

            # 单 chunk 都放不下：按等距间隔保留能放下的 chunk。
            keep = max(content_budget // 200, 1)
            positions = {
                round(index * (len(candidates) - 1) / (keep - 1))
                if keep > 1
                else 0
                for index in range(min(keep, len(candidates)))
            }
            blocks.extend(
                _block(candidates[position], max(content_budget - head, 200))
                for position in sorted(positions)
            )
        return blocks

    def _load_sample_chunks(
        self, documents: list[KnowledgeDocument]
    ) -> list[KnowledgeChunk]:
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
        if not chunks:
            raise ValueError("已解析文档尚未生成可采样的 Chunk")

        grouped: dict[str, list[KnowledgeChunk]] = defaultdict(list)
        for chunk in chunks:
            grouped[chunk.document_id].append(chunk)

        per_document = GRAPH_SCHEMA_SAMPLE_CHUNKS_PER_DOCUMENT
        sampled: list[KnowledgeChunk] = []
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
        graph_kind: str = "general",
    ) -> dict:
        documents = self._documents(document_ids)
        chunks = self._load_sample_chunks(documents)
        sample_blocks = self._sample_blocks(documents, chunks)
        if not sample_blocks:
            raise ValueError("文档样本为空，无法生成 Schema")
        used_chars = sum(len(block) for block in sample_blocks)

        kind_instruction = ""
        if graph_kind != "general":
            profile = get_graph_profile(graph_kind)
            kind_instruction = (
                f"本次只生成【{profile['label']}】Schema，所有类型的 graph_kind 必须是 "
                f"{graph_kind}，并严格遵守该图谱定位：{profile['positioning']}"
            )
        prompt = SCHEMA_INDUCTION_PROMPT.format(
            instructions="\n".join(filter(None, [kind_instruction, (instructions or "").strip()])) or "无",
            samples="\n".join(sample_blocks),
        )
        payload: dict | None = None
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            current_prompt = prompt
            if attempt and last_error is not None:
                current_prompt += (
                    f"\n\n上一次输出解析失败：{last_error}。请修正后只输出有效 JSON。"
                )
            try:
                raw = self.llm.generate(current_prompt)
                payload = _clean_json(raw)
                break
            except Exception as exc:
                last_error = exc
        if payload is None:
            raise last_error or RuntimeError("Schema 归纳失败")

        entity_types = _type_names(payload.get("entity_types"), limit=30)
        relation_types = _type_names(payload.get("relation_types"), limit=40)
        entity_details = _schema_details(
            payload.get("entity_types"), set(entity_types), relation=False
        )
        relation_details = _schema_details(
            payload.get("relation_types"),
            set(relation_types),
            relation=True,
            endpoint_names=set(entity_types),
        )
        base_contract_prompt = (
            profile_schema(graph_kind)["extraction_prompt"]
            if graph_kind != "general" else ""
        )
        extraction_prompt = _enrich_extraction_prompt(
            base_contract_prompt + "\n\n" + str(payload.get("extraction_prompt") or ""),
            entity_details,
            relation_details,
        )
        rationale = str(payload.get("rationale") or "").strip()[:4000]

        sampled_documents = {
            chunk.document_id for chunk in chunks
        }
        all_profiles = profiles_for_schema(
            entity_types, relation_types, entity_details, relation_details,
        ) if graph_kind != "general" else {}
        return {
            "entity_types": entity_types,
            "relation_types": relation_types,
            "graph_profiles": (
                {graph_kind: all_profiles[graph_kind]}
                if graph_kind != "general" else None
            ),
            "extraction_prompt": extraction_prompt or None,
            "source_document_ids": [document.id for document in documents],
            "generation_summary": {
                "source_documents": len(documents),
                "sampled_chunks": len(sample_blocks),
                "sampled_characters": used_chars,
                "sampled_documents": len(sampled_documents),
                "rationale": rationale,
                "entity_type_details": entity_details,
                "relation_type_details": relation_details,
                "graph_kind": graph_kind,
            },
        }

    def _persist_draft(
        self,
        induced: dict,
        *,
        created_by: str | None,
        target_schema_id: str | None = None,
        generation_method: str = "document_llm",
        graph_kind: str = "general",
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
                graph_profiles=induced["graph_profiles"],
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
            graph_kind=graph_kind,
            entity_types=induced["entity_types"],
            relation_types=induced["relation_types"],
            normalization_rules=normalization_rules,
            extraction_prompt=induced["extraction_prompt"],
            graph_profiles=induced["graph_profiles"],
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
        graph_kind: str = "general",
    ):
        induced = self._induce(
            document_ids=document_ids,
            instructions=instructions,
            graph_kind=graph_kind,
        )
        return self._persist_draft(
            induced,
            created_by=created_by,
            target_schema_id=target_schema_id,
            graph_kind=graph_kind,
        )

    def detect_drift(
        self,
        *,
        created_by: str | None,
        document_ids: list[str] | None = None,
        instructions: str | None = None,
        graph_kind: str = "general",
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
        active = governance.active_schema(graph_kind=graph_kind)
        induced = self._induce(
            document_ids=document_ids,
            instructions=instructions,
            graph_kind=graph_kind,
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
            graph_kind=graph_kind,
        )
        result.update({
            "status": "drift_detected",
            "generated_schema_id": draft.id,
            "generated_schema_version": draft.version,
        })
        return result
