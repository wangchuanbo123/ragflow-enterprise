"""
LangGraph 状态对象
定义整个 RAG workflow 共享的数据
"""

from typing import Any, List, TypedDict

from langchain_core.documents import Document


class RAGState(TypedDict, total=False):
    original_query: str
    retrieval_query: str
    retrieval_queries: List[str]
    retrieval_candidates: List[Document]
    query: str
    history: List[dict]
    docs: List[Document]
    context: str
    context_json_file: str
    answer: str
    sources: List[dict]
    timings: dict[str, float]
