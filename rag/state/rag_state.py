"""
LangGraph 状态对象
定义整个 RAG workflow 共享的数据
"""

from typing import TypedDict, List
from langchain_core.documents import Document


class RAGState(TypedDict):

    query: str
    docs: List[Document] # 检索到的相关文档列表，每个文档包含 page_content 和 metadata
    context: str  # 生成回答的上下文，通常是检索到的相关文档内容的拼接
    answer: str
    sources: List[dict]