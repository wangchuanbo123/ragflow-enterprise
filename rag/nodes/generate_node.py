"""
生成回答节点
LangGraph Node
"""

from rag.prompts.prompt_loader import load_prompt
from rag.providers.factory import get_llm_provider
from rag.providers.interfaces import LLMProvider

PROMPT = load_prompt("answer_prompt.txt")

def create_generate_node(llm: LLMProvider):
    def generate_node(state):
        query = state["query"]
        context = state["context"]

        prompt = PROMPT.format(
            query=query,
            context=context,
        )

        return {
            "answer": llm.generate(prompt),
        }

    return generate_node


def generate_node(state):
    return create_generate_node(get_llm_provider())(state)
