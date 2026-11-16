"""实体关系抽取器。

使用 LLM 从文档片段中提取实体、别名、关系和原文证据。
支持 JSON 解析、围栏剥离、校验和一次修复重试。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata

from rag.knowledge_graph.schemas import (
    VALID_ENTITY_TYPES,
    VALID_PREDICATES,
    ChunkExtraction,
    ExtractedEntity,
    ExtractedRelation,
)

logger = logging.getLogger(__name__)


def _reference_key(value: object) -> str:
    """Normalize an entity reference without changing the stored display name."""
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _generated_local_id(index: int, used_ids: set[str]) -> str:
    candidate = f"e{index}"
    while candidate in used_ids:
        index += 1
        candidate = f"e{index}"
    return candidate


EXTRACTION_PROMPT = """你是知识图谱实体关系抽取助手。

从以下文档片段中提取实体和关系。

规则：
1. 只提取文档中明确出现的信息，不要凭常识补充
2. 实体类型必须是以下之一：{entity_types}
3. 关系谓词必须是以下之一：{relation_types}
4. evidence 必须是文档原文中的短句，不能改写
5. 使用稳定、完整的实体名称；简称和缩写放入 aliases，不要把“系统”“功能”“模块”等无明确指代的词单独建实体
6. 同一主语、谓词、宾语只返回一次；严格区分关系方向，不要同时返回同义的正反关系
7. evidence 应尽量同时包含主语和宾语的原文指代；如果文档中没有明确的实体关系，返回空列表

文档片段（chunk_id: {chunk_id}）：
{content}

只输出 JSON，不要输出其他内容。格式：
{{"chunk_id": "{chunk_id}", "entities": [{{"local_id": "e1", "name": "实体名", "type": "MODULE", "aliases": ["别名"], "description": "描述"}}], "relations": [{{"subject": "e1", "predicate": "DEPENDS_ON", "object": "e2", "evidence": "原文短句", "confidence": 0.9}}]}}"""


def _strip_markdown_and_think(text: str) -> str:
    """去除 Markdown 代码围栏和 <think>...</think>。"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text.strip())
    return text.strip()


def parse_extraction_response(
    raw_response: str,
    chunk_id: str,
    chunk_content: str,
    entity_types: set[str] | frozenset[str] | None = None,
    relation_types: set[str] | frozenset[str] | None = None,
    relation_constraints: dict[str, tuple[set[str], set[str]]] | None = None,
    endpoint_variants: dict[str, list[tuple[set[str], set[str]]]] | None = None,
) -> ChunkExtraction:
    """解析 LLM 输出为校验后的 ChunkExtraction。

    执行以下校验：
    1. 去除围栏和 think 标签
    2. JSON 解析
    3. 实体类型白名单
    4. 谓词白名单
    5. local_id 引用有效性
    6. evidence 在原文中可找到
    """
    cleaned = _strip_markdown_and_think(raw_response)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc}") from exc

    data.setdefault("chunk_id", chunk_id)

    allowed_entity_types = entity_types or VALID_ENTITY_TYPES
    allowed_relation_types = relation_types or VALID_PREDICATES

    # Validate entities. Some compatible models omit local_id and reference
    # relation endpoints by entity name even when the prompt asks otherwise.
    # Normalize that common, unambiguous representation before validation.
    valid_entities: dict[str, ExtractedEntity] = {}
    entity_references: dict[str, set[str]] = {}
    raw_entities = data.get("entities", [])
    if not isinstance(raw_entities, list):
        raw_entities = []
    rejected_entities = 0
    used_ids: set[str] = set()
    for index, ent_data in enumerate(raw_entities, start=1):
        if not isinstance(ent_data, dict):
            rejected_entities += 1
            continue
        normalized = dict(ent_data)
        local_id = str(
            normalized.get("local_id") or normalized.get("id") or ""
        ).strip()
        if not local_id or local_id in used_ids:
            local_id = _generated_local_id(index, used_ids)
        normalized["local_id"] = local_id
        normalized["type"] = str(normalized.get("type") or "").strip().upper()
        try:
            ent = ExtractedEntity(**normalized)
        except Exception as exc:
            rejected_entities += 1
            logger.debug("Rejected malformed entity in chunk %s: %s", chunk_id, exc)
            continue
        if ent.type not in allowed_entity_types:
            rejected_entities += 1
            continue
        used_ids.add(ent.local_id)
        valid_entities[ent.local_id] = ent

        references = [ent.local_id, ent.name, *(ent.aliases or [])]
        for reference in references:
            key = _reference_key(reference)
            if key:
                entity_references.setdefault(key, set()).add(ent.local_id)

    # Validate relations
    valid_relations: list[ExtractedRelation] = []
    content_stripped = re.sub(r"\s+", "", chunk_content)

    raw_relations = data.get("relations", [])
    if not isinstance(raw_relations, list):
        raw_relations = []
    rejected_relations = 0
    for rel_data in raw_relations:
        if not isinstance(rel_data, dict):
            rejected_relations += 1
            continue
        normalized = dict(rel_data)
        normalized["predicate"] = str(
            normalized.get("predicate") or ""
        ).strip().upper()
        for endpoint in ("subject", "object"):
            reference = _reference_key(normalized.get(endpoint))
            matches = entity_references.get(reference, set())
            if len(matches) == 1:
                normalized[endpoint] = next(iter(matches))
        try:
            rel = ExtractedRelation(**normalized)
        except Exception as exc:
            rejected_relations += 1
            logger.debug("Rejected malformed relation in chunk %s: %s", chunk_id, exc)
            continue
        if rel.predicate not in allowed_relation_types:
            rejected_relations += 1
            continue
        if rel.subject not in valid_entities or rel.object not in valid_entities:
            rejected_relations += 1
            continue
        if rel.subject == rel.object:
            rejected_relations += 1
            continue
        variants = (endpoint_variants or {}).get(rel.predicate)
        if variants and not any((not sources or valid_entities[rel.subject].type in sources)
                                and (not targets or valid_entities[rel.object].type in targets)
                                for sources, targets in variants):
            rejected_relations += 1
            continue
        constraint = (relation_constraints or {}).get(rel.predicate)
        if constraint is not None:
            source_types, target_types = constraint
            subject_type = valid_entities[rel.subject].type
            object_type = valid_entities[rel.object].type
            if source_types and subject_type not in source_types:
                rejected_relations += 1
                logger.debug(
                    "Rejected relation %s with invalid source type %s",
                    rel.predicate,
                    subject_type,
                )
                continue
            if target_types and object_type not in target_types:
                rejected_relations += 1
                logger.debug(
                    "Rejected relation %s with invalid target type %s",
                    rel.predicate,
                    object_type,
                )
                continue
        # evidence must be findable in content (whitespace-insensitive)
        evidence_stripped = re.sub(r"\s+", "", rel.evidence)
        if evidence_stripped and evidence_stripped not in content_stripped:
            rejected_relations += 1
            logger.debug("Rejected evidence not found in chunk %s: %s", chunk_id, rel.evidence[:50])
            continue
        valid_relations.append(rel)

    if rejected_entities or rejected_relations:
        logger.warning(
            "Graph extraction normalized/rejected output for chunk %s: "
            "entities=%d/%d relations=%d/%d",
            chunk_id,
            rejected_entities,
            len(raw_entities),
            rejected_relations,
            len(raw_relations),
        )

    return ChunkExtraction(
        chunk_id=chunk_id,
        entities=list(valid_entities.values()),
        relations=valid_relations,
    )


class GraphExtractor:
    """实体关系抽取器。"""

    def __init__(
        self,
        llm=None,
        max_retries: int = 1,
        max_content_chars: int | None = None,
    ):
        self._llm = llm
        self.max_retries = max_retries
        if max_content_chars is None:
            from app.core.config import GRAPH_EXTRACTION_MAX_CHARS

            max_content_chars = GRAPH_EXTRACTION_MAX_CHARS
        self.max_content_chars = max_content_chars
        self.entity_types = set(VALID_ENTITY_TYPES)
        self.relation_types = set(VALID_PREDICATES)
        self.relation_constraints: dict[str, tuple[set[str], set[str]]] = {}
        self.relation_constraint_variants = {}
        self.prompt_prefix: str | None = None

    def configure_schema(
        self,
        *,
        entity_types: list[str] | None = None,
        relation_types: list[str] | None = None,
        relation_constraints: list[dict] | None = None,
        prompt_prefix: str | None = None,
    ) -> None:
        """Apply an active registry schema without coupling it to a provider."""
        self.entity_types = set(entity_types or VALID_ENTITY_TYPES)
        self.relation_types = set(relation_types or VALID_PREDICATES)
        self.relation_constraints = {}
        self.relation_constraint_variants = {}
        for item in relation_constraints or []:
            predicate = str(item.get("name") or "").strip().upper()
            if predicate not in self.relation_types:
                continue
            variants = item.get("endpoint_variants")
            if not isinstance(variants, list) or not variants:
                variants = [item]
            pairs = self.relation_constraint_variants.setdefault(predicate, [])
            for variant in variants:
                # Preserve explicit endpoints even if a later draft removed a
                # type. An unavailable endpoint means no legal match, never an
                # empty-set wildcard that broadens the relationship.
                source = {str(value).strip().upper() for value in variant.get("source_types", []) if str(value).strip()}
                target = {str(value).strip().upper() for value in variant.get("target_types", []) if str(value).strip()}
                pairs.append((source, target))
            self.relation_constraints[predicate] = (
                set().union(*(a for a, b in pairs)) if all(a for a, b in pairs) else set(),
                set().union(*(b for a, b in pairs)) if all(b for a, b in pairs) else set(),
            )
        self.prompt_prefix = prompt_prefix

    @property
    def llm(self):
        if self._llm is None:
            from rag.providers.factory import get_graph_extraction_llm_provider
            self._llm = get_graph_extraction_llm_provider()
        return self._llm

    def extract_from_chunk(
        self,
        chunk_id: str,
        chunk_content: str,
        metadata: dict | None = None,
        *,
        llm=None,
    ) -> ChunkExtraction:
        """从单个 chunk 提取实体关系。失败时抛出异常。"""
        prompt = EXTRACTION_PROMPT.format(
            chunk_id=chunk_id,
            content=chunk_content[: self.max_content_chars],
            entity_types=", ".join(sorted(self.entity_types)),
            relation_types=", ".join(sorted(self.relation_types)),
        )
        if self.prompt_prefix:
            prompt = self.prompt_prefix.strip() + "\n\n" + prompt

        active_llm = llm or self.llm
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            current_prompt = prompt
            if attempt:
                current_prompt += (
                    f"\n\n上一次输出解析失败：{last_error}。请只输出有效 JSON。"
                )
            try:
                raw = active_llm.generate(current_prompt)
                return parse_extraction_response(
                    raw,
                    chunk_id,
                    chunk_content,
                    self.entity_types,
                    self.relation_types,
                    self.relation_constraints,
                    self.relation_constraint_variants,
                )
            except Exception as exc:
                last_error = exc

        raise last_error or RuntimeError("Extraction failed")

    def extract_batch(
        self,
        chunks: list[dict],
        *,
        llm=None,
    ) -> list[ChunkExtraction]:
        """Extract multiple chunks with one LLM request."""
        if not chunks:
            return []
        active_llm = llm or self.llm

        def make_prompt(
            pending_chunks: list[dict],
            retry_error: Exception | None = None,
        ) -> str:
            payload = [
                {
                    "chunk_id": chunk["chunk_id"],
                    "content": chunk["content"][: self.max_content_chars],
                }
                for chunk in pending_chunks
            ]
            prompt = (
                "你是知识图谱实体关系抽取助手。请严格遵守以下规则：\n"
                "1. 只提取原文明确出现的信息，不得补充常识。\n"
                "2. entity type 只能使用："
                + ", ".join(sorted(self.entity_types))
                + "。\n3. predicate 只能使用："
                + ", ".join(sorted(self.relation_types))
                + "。\n4. evidence 必须是对应 chunk 中可找到的原文短句。\n"
                "5. 每个输入 chunk 都必须返回一个结果，没有内容时返回空列表。\n"
                "6. 使用稳定完整的实体名称，简称和缩写放入 aliases；不要抽取无明确指代的泛化实体。\n"
                "7. 同一三元组只返回一次，严格遵守关系方向；evidence 应尽量包含两端实体的原文指代。\n"
                "8. 每个实体必须包含唯一 local_id（如 e1、e2）；关系 subject/object 必须引用 local_id，不能填写实体名称。\n"
                '只输出 JSON：{"chunks":[{"chunk_id":"...",'
                '"entities":[{"local_id":"e1","name":"实体名","type":"类型",'
                '"aliases":[],"description":"描述"}],'
                '"relations":[{"subject":"e1","predicate":"关系类型",'
                '"object":"e2","evidence":"原文短句","confidence":0.9}]}]}\n\n输入：\n'
                + json.dumps(payload, ensure_ascii=False)
            )
            if self.prompt_prefix:
                prompt = self.prompt_prefix.strip() + "\n\n" + prompt
            if retry_error is not None:
                prompt += (
                    f"\n\n上一次输出不完整或解析失败：{retry_error}。"
                    "本次只需返回上面仍缺失的 chunk，并严格使用其原始 chunk_id。"
                )
            return prompt

        last_error: Exception | None = None
        pending = {chunk["chunk_id"]: chunk for chunk in chunks}
        extracted: dict[str, ChunkExtraction] = {}
        for attempt in range(self.max_retries + 1):
            current_prompt = make_prompt(
                list(pending.values()), last_error if attempt else None
            )
            try:
                raw = active_llm.generate(current_prompt)
                cleaned = _strip_markdown_and_think(raw)
                data = json.loads(cleaned)
                raw_chunks = data.get("chunks")
                if not isinstance(raw_chunks, list):
                    raise ValueError("Batch extraction response is missing 'chunks'")

                response_by_id = {
                    str(item.get("chunk_id")): item
                    for item in raw_chunks
                    if isinstance(item, dict) and item.get("chunk_id")
                }
                accepted = 0
                parse_errors: list[str] = []
                for chunk_id, chunk in list(pending.items()):
                    item = response_by_id.get(chunk_id)
                    if item is None:
                        continue
                    try:
                        extracted[chunk_id] = parse_extraction_response(
                            json.dumps(
                                item,
                                ensure_ascii=False,
                            ),
                            chunk_id,
                            chunk["content"],
                            self.entity_types,
                            self.relation_types,
                            self.relation_constraints,
                            self.relation_constraint_variants,
                        )
                    except Exception as exc:
                        parse_errors.append(f"{chunk_id}: {exc}")
                        continue
                    del pending[chunk_id]
                    accepted += 1

                if not pending:
                    return [extracted[chunk["chunk_id"]] for chunk in chunks]
                missing = ", ".join(pending)
                details = "; ".join(parse_errors[:3])
                last_error = ValueError(
                    f"Batch extraction missing or invalid chunk IDs: {missing}"
                    + (f"; {details}" if details else "")
                )
                if accepted:
                    logger.warning(
                        "Graph batch response was partial; accepted %d and will retry %d chunks",
                        accepted,
                        len(pending),
                    )
            except Exception as exc:
                last_error = exc

        raise last_error or RuntimeError("Batch extraction failed")
