"""Relation predicate vocabulary used by lexical and vector retrieval."""

from __future__ import annotations

import re
import unicodedata


RELATION_VOCABULARY: dict[str, dict[str, object]] = {
    "CONTAINS": {
        "label": "包含",
        "description": "主体包含、包括或由客体组成",
        "aliases": ("包含", "包括", "组成", "contains", "contain", "includes", "include"),
    },
    "PART_OF": {
        "label": "属于",
        "description": "主体是客体的一部分或隶属于客体",
        "aliases": ("属于", "隶属于", "组成部分", "part of", "belongs to"),
    },
    "DEPENDS_ON": {
        "label": "依赖",
        "description": "主体依赖、要求或取决于客体",
        "aliases": (
            "依赖", "依赖于", "需要", "要求", "取决于",
            "depends on", "depend on", "requires", "required by",
            "dependency", "dependencies",
        ),
    },
    "CALLS": {
        "label": "调用",
        "description": "主体调用或请求客体",
        "aliases": ("调用", "被调用", "calls", "call", "invokes", "invoke"),
    },
    "PROVIDES": {
        "label": "提供",
        "description": "主体向外提供或暴露客体能力",
        "aliases": ("提供", "暴露", "供给", "provides", "provide", "exposes", "expose"),
    },
    "IMPLEMENTS": {
        "label": "实现",
        "description": "主体实现或落地客体定义",
        "aliases": ("实现", "落地", "implements", "implement"),
    },
    "SATISFIES": {
        "label": "满足",
        "description": "主体满足或符合客体要求",
        "aliases": ("满足", "符合", "satisfies", "satisfy", "meets", "meet"),
    },
    "VERIFIED_BY": {
        "label": "被验证",
        "description": "主体由客体验证、校验或测试覆盖",
        "aliases": ("验证", "被验证", "校验", "测试覆盖", "verified by", "validated by", "tested by"),
    },
    "DEPLOYED_ON": {
        "label": "部署于",
        "description": "主体部署或运行在客体环境",
        "aliases": ("部署于", "部署在", "运行于", "运行在", "deployed on", "runs on"),
    },
    "OWNED_BY": {
        "label": "归属",
        "description": "主体由客体负责、维护或拥有",
        "aliases": ("负责", "归属", "维护方", "负责人", "owned by", "maintained by"),
    },
    "USES": {
        "label": "使用",
        "description": "主体使用、采用或利用客体",
        "aliases": ("使用", "采用", "利用", "uses", "use", "utilizes", "utilize"),
    },
    "RELATED_TO": {
        "label": "相关",
        "description": "主体与客体存在一般关联",
        "aliases": ("相关", "关联", "有关", "related to", "associated with"),
    },
}


def normalize_relation_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def relation_terms(
    predicate: str,
    vocabulary: dict[str, dict[str, object]] | None = None,
) -> list[str]:
    item = (vocabulary or RELATION_VOCABULARY).get(predicate, {})
    values = [predicate.replace("_", " "), str(item.get("label", ""))]
    values.extend(str(value) for value in item.get("aliases", ()))
    return list(dict.fromkeys(value for value in values if value))


def relation_description(
    predicate: str,
    vocabulary: dict[str, dict[str, object]] | None = None,
) -> str:
    return str((vocabulary or RELATION_VOCABULARY).get(predicate, {}).get("description", ""))


def relation_query_scores(
    query: str,
    vocabulary: dict[str, dict[str, object]] | None = None,
) -> dict[str, float]:
    """Return predicates explicitly expressed by a multilingual query."""
    normalized_query = normalize_relation_text(query)
    if not normalized_query:
        return {}

    scores: dict[str, float] = {}
    resolved_vocabulary = vocabulary or RELATION_VOCABULARY
    for predicate in resolved_vocabulary:
        best = 0.0
        for term in relation_terms(predicate, resolved_vocabulary):
            normalized_term = normalize_relation_text(term)
            if not normalized_term:
                continue
            if normalized_query == normalized_term:
                best = 1.0
                break
            if _contains_term(normalized_query, normalized_term):
                coverage = len(normalized_term) / max(len(normalized_query), 1)
                best = max(best, 0.80 + min(0.18, coverage * 0.18))
        if best:
            scores[predicate] = best
    return scores


def _contains_term(query: str, term: str) -> bool:
    if any("\u4e00" <= char <= "\u9fff" for char in term):
        return term in query
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", query) is not None
