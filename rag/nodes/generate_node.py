"""
生成回答节点
LangGraph Node
"""

from langchain_community.llms import Ollama
from rag.prompts.prompt_loader import load_prompt
from app.core.config import LLM_MODEL

llm = Ollama(model=LLM_MODEL)

PROMPT = load_prompt("answer_prompt.txt")

def generate_node(state):

    query = state["query"]
    context = state["context"]

    prompt = PROMPT.format(
        query=query,
        context=context
    )

    answer = llm.invoke(prompt)

    return {
        "answer": answer
    }