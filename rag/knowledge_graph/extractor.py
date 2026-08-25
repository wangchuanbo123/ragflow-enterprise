"""实体关系抽取器。

使用 LLM 从文档片段中提取实体、别名、关系和原文证据。
支持 JSON 解析、围栏剥离、校验和一次修复重试。
"""

from __future__ import annotations

import json
import logging
import re

from rag.knowledge_graph.schemas import (
    VALID_ENTITY_TYPES,
    VALID_PREDICATES,
    ChunkExtraction,
    ExtractedEntity,
    ExtractedRelation,
)

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """你是知识图谱实体关系抽取助手。

从以下文档片段中提取实体和关系。

规则：
1. 只提取文档中明确出现的信息，不要凭常识补充
2. 实体类型必须是以下之一：{entity_types}
3. 关系谓词必须是以下之一：{relation_types}
4. evidence 必须是文档原文中的短句，不能改写
5. 如果文档中没有明确的实体关系，返回空列表

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

    # Validate entities
    valid_entities: dict[str, ExtractedEntity] = {}
    for ent_data in data.get("entities", []):
        try:
            ent = ExtractedEntity(**ent_data)
        except Exception:
            continue
        if ent.type not in allowed_entity_types:
            continue
        valid_entities[ent.local_id] = ent

    # Validate relations
    valid_relations: list[ExtractedRelation] = []
    content_stripped = re.sub(r"\s+", "", chunk_content)

    for rel_data in data.get("relations", []):
        try:
            rel = ExtractedRelation(**rel_data)
        except Exception:
            continue
        if rel.predicate not in allowed_relation_types:
            continue
        if rel.subject not in valid_entities or rel.object not in valid_entities:
            continue
        if rel.subject == rel.object:
            continue
        # evidence must be findable in content (whitespace-insensitive)
        evidence_stripped = re.sub(r"\s+", "", rel.evidence)
        if evidence_stripped and evidence_stripped not in content_stripped:
            logger.debug("Rejected evidence not found in chunk %s: %s", chunk_id, rel.evidence[:50])
            continue
        valid_relations.append(rel)

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
        self.prompt_prefix: str | None = None

    def configure_schema(
        self,
        *,
        entity_types: list[str] | None = None,
        relation_types: list[str] | None = None,
        prompt_prefix: str | None = None,
    ) -> None:
        """Apply an active registry schema without coupling it to a provider."""
        self.entity_types = set(entity_types or VALID_ENTITY_TYPES)
        self.relation_types = set(relation_types or VALID_PREDICATES)
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

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            current_prompt = prompt
            if attempt:
                current_prompt += (
                    f"\n\n上一次输出解析失败：{last_error}。请只输出有效 JSON。"
                )
            try:
                raw = self.llm.generate(current_prompt)
                return parse_extraction_response(
                    raw,
                    chunk_id,
                    chunk_content,
                    self.entity_types,
                    self.relation_types,
                )
            except Exception as exc:
                last_error = exc

        raise last_error or RuntimeError("Extraction failed")

    def extract_batch(
        self,
        chunks: list[dict],
    ) -> list[ChunkExtraction]:
        """Extract multiple chunks with one LLM request."""
        if not chunks:
            return []

        payload = [
            {
                "chunk_id": chunk["chunk_id"],
                "content": chunk["content"][: self.max_content_chars],
            }
            for chunk in chunks
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
            '只输出 JSON：{"chunks":[{"chunk_id":"...",'
            '"entities":[],"relations":[]}]}\n\n输入：\n'
            + json.dumps(payload, ensure_ascii=False)
        )
        if self.prompt_prefix:
            prompt = self.prompt_prefix.strip() + "\n\n" + prompt

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            current_prompt = prompt
            if attempt:
                current_prompt += (
                    f"\n\n上一次输出解析失败：{last_error}。请只输出有效 JSON。"
                )
            try:
                raw = self.llm.generate(current_prompt)
                cleaned = _strip_markdown_and_think(raw)
                data = json.loads(cleaned)
                raw_chunks = data.get("chunks")
                if not isinstance(raw_chunks, list):
                    raise ValueError("Batch extraction response is missing 'chunks'")

                response_by_id = {
                    item.get("chunk_id"): item
                    for item in raw_chunks
                    if isinstance(item, dict) and item.get("chunk_id")
                }
                expected_ids = {chunk["chunk_id"] for chunk in chunks}
                if set(response_by_id) != expected_ids:
                    raise ValueError(
                        "Batch extraction chunk IDs do not match the request"
                    )

                return [
                    parse_extraction_response(
                        json.dumps(
                            response_by_id[chunk["chunk_id"]],
                            ensure_ascii=False,
                        ),
                        chunk["chunk_id"],
                        chunk["content"],
                        self.entity_types,
                        self.relation_types,
                    )
                    for chunk in chunks
                ]
            except Exception as exc:
                last_error = exc

        raise last_error or RuntimeError("Batch extraction failed")
