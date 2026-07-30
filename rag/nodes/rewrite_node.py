"""
Query Rewrite Node

改写基于原始问题，不覆盖原始问题。
始终保留 original_query，改写成功且与原问题实质不同时加入 retrieval_queries。
"""

import logging

from rag.prompts.prompt_loader import load_prompt
from rag.providers.interfaces import LLMProvider
from rag.utils.history import format_history

logger = logging.getLogger(__name__)

PROMPT = load_prompt("rewrite_prompt.txt")
CHAT_PROMPT = load_prompt("rewrite_chat_prompt.txt")


def _build_prompt(state, base_prompt: str) -> str:
    original = state.get("original_query") or state.get("query", "")
    history = state.get("history") or []
    history_block = format_history(history)
    if history_block:
        return CHAT_PROMPT.format(query=original, history=history_block)
    return base_prompt.format(query=original)


def _normalize(text: str) -> str:
    return text.strip().replace(" ", "").replace("\n", "").replace("\r", "")


def create_rewrite_node(llm: LLMProvider):
    def rewrite_node(state):
        original = state.get("original_query") or state.get("query", "")
        prompt = _build_prompt(state, PROMPT)

        retrieval_queries = [original]
        rewritten = ""

        try:
            rewritten = llm.generate(prompt).strip()
        except Exception as exc:
            logger.warning("Query rewrite failed, using original only: %s", exc)

        if rewritten and _normalize(rewritten) != _normalize(original):
            retrieval_queries = [original, rewritten]

        return {
            "original_query": original,
            "retrieval_query": rewritten or original,
            "retrieval_queries": retrieval_queries,
        }

    return rewrite_node


def rewrite_node(state):
    from rag.providers.factory import get_llm_provider
    return create_rewrite_node(get_llm_provider())(state)
