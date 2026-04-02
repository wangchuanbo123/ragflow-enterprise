"""
Query Rewrite Node
作用：
使用 LLM 对用户问题进行改写，提高检索效果
"""

from langchain_community.llms import Ollama
from rag.prompts.prompt_loader import load_prompt
from app.core.config import LLM_MODEL

llm = Ollama(model=LLM_MODEL)

PROMPT = load_prompt("rewrite_prompt.txt")

def rewrite_node(state):

    query = state["query"]

    prompt = PROMPT.format(query=query)

    new_query = llm.invoke(prompt)

    return {
        "query": new_query.strip()
    }