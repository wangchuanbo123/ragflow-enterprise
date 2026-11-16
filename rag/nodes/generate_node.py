"""
生成回答节点
Application workflow step

使用原始问题生成答案（而非改写后的问题）。
"""

from typing import Iterator

from rag.prompts.prompt_loader import load_prompt
from rag.providers.factory import get_llm_provider
from rag.providers.interfaces import LLMProvider
from rag.utils.history import format_history
from rag.answer_validation import NO_EVIDENCE_ANSWER, evidence_unavailable, validate_answer, filter_citation_stream

PROMPT = load_prompt("answer_prompt.txt")
CHAT_PROMPT = load_prompt("answer_chat_prompt.txt")


def _build_prompt(state, base_prompt: str) -> str:
    original = state.get("original_query") or state.get("query", "")
    context = state.get("context", "")
    history = state.get("history") or []
    history_block = format_history(history)
    if history_block:
        return CHAT_PROMPT.format(query=original, context=context, history=history_block)
    return base_prompt.format(query=original, context=context)


def create_generate_node(llm: LLMProvider):
    def generate_node(state):
        route = dict(state.get("route") or {})
        if evidence_unavailable(state.get("sources"), route):
            answer = NO_EVIDENCE_ANSWER
        else:
            answer = llm.generate(_build_prompt(state, PROMPT))
        answer, validation = validate_answer(answer, state.get("sources"), route)
        route["citation_validation"] = validation
        return {
            "answer": answer, "route": route,
        }

    return generate_node


def create_stream_generate_node(llm: LLMProvider):
    def stream_generate_node(state) -> Iterator[str]:
        if evidence_unavailable(state.get("sources"), state.get("route")):
            yield NO_EVIDENCE_ANSWER
            return
        prompt = _build_prompt(state, PROMPT)
        yield from filter_citation_stream(llm.stream(prompt), state.get("sources"), state.get("route"))

    return stream_generate_node


def generate_node(state):
    return create_generate_node(get_llm_provider())(state)
