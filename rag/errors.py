"""RAG 领域异常。

Provider 层保留原始异常链，Service 层转换为领域异常，
API 层转换为统一错误响应。
"""

from __future__ import annotations


class RAGError(Exception):
    """所有 RAG 领域异常的基类。"""

    code = "RAG_ERROR"
    status_code = 500

    def __init__(self, message: str, *, cause: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.cause = cause


class LLMProviderError(RAGError):
    code = "LLM_UNAVAILABLE"
    status_code = 503


class EmbeddingProviderError(RAGError):
    code = "EMBEDDING_UNAVAILABLE"
    status_code = 503


class RerankerError(RAGError):
    code = "RERANKER_UNAVAILABLE"
    status_code = 503


class VectorStoreError(RAGError):
    code = "VECTOR_STORE_UNAVAILABLE"
    status_code = 503


class RetrievalError(RAGError):
    code = "RETRIEVAL_FAILED"
    status_code = 500


class IndexingError(RAGError):
    code = "INDEX_FAILED"
    status_code = 500


class IndexBusyError(RAGError):
    code = "INDEX_BUSY"
    status_code = 409


class DocumentValidationError(RAGError):
    code = "DOCUMENT_UNSUPPORTED"
    status_code = 400


class DocumentTooLargeError(RAGError):
    code = "DOCUMENT_TOO_LARGE"
    status_code = 413


class RAGBusyError(RAGError):
    code = "RAG_BUSY"
    status_code = 429


class RAGTimeoutError(RAGError):
    code = "RAG_TIMEOUT"
    status_code = 504
