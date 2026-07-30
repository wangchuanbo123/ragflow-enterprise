"""错误和并发测试。"""

import pytest

from rag.errors import (
    DocumentValidationError,
    RAGBusyError,
    RAGError,
    RAGTimeoutError,
)


def test_error_codes_are_distinct():
    errors = [
        RAGError("e"),
        DocumentValidationError("v"),
        RAGBusyError("b"),
        RAGTimeoutError("t"),
    ]
    codes = [e.code for e in errors]
    assert len(codes) == len(set(codes))


def test_error_preserves_cause():
    original = ValueError("original")
    err = RAGError("wrapped", cause=original)
    assert err.cause is original


def test_rag_concurrency_allows_within_limit():
    from app.services.rag_concurrency import rag_concurrency
    with rag_concurrency():
        pass  # Should succeed


def test_document_validation_error_is_rag_error():
    err = DocumentValidationError("bad doc")
    assert isinstance(err, RAGError)
    assert err.status_code == 400
