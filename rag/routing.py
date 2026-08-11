"""Explainable rule-first retrieval routing with an optional LLM fallback."""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.config import RETRIEVAL_ROUTER_MODE, ROUTER_LLM_ENABLED

_GRAPH_TERMS = {
    "关系", "关联", "联系", "依赖", "影响", "原因", "导致", "路径", "上下游",
    "谁和谁", "相关实体", "relationship", "related", "depend", "impact",
    "cause", "path", "connected", "upstream", "downstream",
}
_CODE_TERMS = {
    "代码", "函数", "类", "方法", "调用", "模块", "接口", "源码", "实现",
    "code", "function", "class", "method", "call", "module", "symbol",
    "implementation",
}


def _contains_term(query: str, terms: set[str]) -> bool:
    lowered = query.lower()
    return any(term in lowered for term in terms)


def _parse_llm_channels(text: str) -> list[str]:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S | re.I)
    if fenced:
        cleaned = fenced.group(1).strip()
    payload = json.loads(cleaned)
    channels = payload.get("channels", []) if isinstance(payload, dict) else []
    return [item for item in channels if item in {"text", "graph", "code"}]


def route_query(
    query: str,
    *,
    explicit_channels: list[str] | None = None,
    scope: dict[str, Any] | None = None,
    router_llm=None,
    graph_available: bool = False,
    code_available: bool = False,
) -> dict:
    requested = list(dict.fromkeys(explicit_channels or []))
    reasons: list[str] = []
    method = "explicit" if requested else "rules"
    confidence = 1.0 if requested else 0.75

    if not requested:
        requested = ["text"]
        if _contains_term(query, _GRAPH_TERMS):
            requested.append("graph")
            reasons.append("问题包含关系、依赖、影响或路径意图")
            confidence = 0.9
        # "module/模块" alone is a domain concept as often as a code symbol.
        # More specific terms such as call, class, source or implementation
        # still route module-related source questions to the code channel.
        if _contains_term(query, _CODE_TERMS - {"module", "模块"}):
            requested.append("code")
            reasons.append("问题包含代码、符号或调用意图")
            confidence = 0.9

        if (
            method == "rules"
            and RETRIEVAL_ROUTER_MODE == "hybrid"
            and ROUTER_LLM_ENABLED
            and router_llm is not None
            and len(requested) == 1
        ):
            prompt = (
                "你是检索路由器。仅输出 JSON，例如 "
                '{"channels":["text","graph"]}。'
                "可选通道为 text、graph、code。关系与多跳问题选择 graph，"
                "代码符号和调用问题选择 code，普通文档问题选择 text。\n"
                f"问题：{query}"
            )
            try:
                llm_channels = _parse_llm_channels(router_llm.generate(prompt))
                if llm_channels:
                    requested = list(dict.fromkeys(llm_channels))
                    method = "hybrid_llm"
                    confidence = 0.7
                    reasons.append("规则未形成强意图，采用路由模型结构化回退")
            except Exception as exc:
                reasons.append(f"路由模型回退失败，继续使用规则结果：{type(exc).__name__}")

    available = {
        "text": True,
        "graph": graph_available,
        "code": code_available,
    }
    selected = [channel for channel in requested if available.get(channel, False)]
    unavailable = [channel for channel in requested if not available.get(channel, False)]
    if unavailable:
        reasons.append(f"通道尚未就绪：{', '.join(unavailable)}")
    if not selected:
        selected = ["text"]
        reasons.append("请求通道不可用，降级到文本检索")
        confidence = min(confidence, 0.5)
    if not reasons:
        reasons.append("普通知识问答，使用文本向量与 BM25 混合检索")

    return {
        "method": method,
        "selected_channels": selected,
        "requested_channels": requested,
        "unavailable_channels": unavailable,
        "confidence": confidence,
        "reasons": reasons,
        "scope": scope or {
            "document_ids": [],
            "tag_ids": [],
            "folder_id": None,
            "metadata": {},
        },
    }
