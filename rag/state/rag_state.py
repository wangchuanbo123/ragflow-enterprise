"""
LangGraph 状态对象
定义整个 RAG workflow 共享的数据
"""

import operator
from typing import Annotated, Any, List, TypedDict

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
    retrieval_options: dict
    route: dict
    allowed_document_ids: set[str] | None
    text_docs: List[Document]
    graph_docs: List[Document]
    code_docs: List[Document]
    text_channel_result: dict
    graph_channel_result: dict
    code_channel_result: dict
    channel_results: dict[str, dict]
    parallel_markers: Annotated[List[str], operator.add]
    generation_mode: str
