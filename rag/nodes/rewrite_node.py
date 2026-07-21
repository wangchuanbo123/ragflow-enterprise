"""
Query Rewrite Node
作用：
使用 LLM 对用户问题进行改写，提高检索效果
"""

from rag.prompts.prompt_loader import load_prompt
from rag.providers.factory import get_llm_provider
from rag.providers.interfaces import LLMProvider

PROMPT = load_prompt("rewrite_prompt.txt")

def create_rewrite_node(llm: LLMProvider):
    def rewrite_node(state):
        prompt = PROMPT.format(query=state["query"])

        return {
            "query": llm.generate(prompt).strip(),
        }

    return rewrite_node


def rewrite_node(state):
    return create_rewrite_node(get_llm_provider())(state)
