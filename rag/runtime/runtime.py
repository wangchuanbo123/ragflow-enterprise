"""Lazy runtime dependency container for the RAG workflow."""

from dataclasses import dataclass
from functools import lru_cache

from app.core.config import DOC_DIR, VECTOR_DB_DIR
from rag.loaders.document_loader import load_documents
from rag.providers.factory import (
    get_embedding_provider,
    get_llm_provider,
    get_reranker,
    get_vector_store_provider,
)
from rag.providers.interfaces import LLMProvider, Reranker, Retriever
from rag.retrievers.hybrid_retriever import create_hybrid_retriever


@dataclass(frozen=True)
class RAGRuntime:
    llm: LLMProvider
    retriever: Retriever
    reranker: Reranker


@lru_cache(maxsize=1)
def get_runtime() -> RAGRuntime:
    print("初始化 RAG 系统...")

    embedding = get_embedding_provider().get_model()
    vector_db = get_vector_store_provider().load(
        embedding=embedding,
        persist_dir=str(VECTOR_DB_DIR),
    )
    docs = load_documents(str(DOC_DIR))
    retriever = create_hybrid_retriever(vector_db, docs)

    runtime = RAGRuntime(
        llm=get_llm_provider(),
        retriever=retriever,
        reranker=get_reranker(),
    )

    print("RAG 初始化完成")
    return runtime
