"""RAG 并发控制：应用级 Semaphore 限制同时运行的完整 RAG 请求。"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Generator

from app.core.config import RAG_MAX_CONCURRENCY, RAG_QUEUE_TIMEOUT_SECONDS
from rag.errors import RAGBusyError

logger = logging.getLogger(__name__)

_semaphore: threading.BoundedSemaphore | None = None
_lock = threading.Lock()


def get_rag_semaphore() -> threading.BoundedSemaphore:
    global _semaphore
    with _lock:
        if _semaphore is None:
            _semaphore = threading.BoundedSemaphore(RAG_MAX_CONCURRENCY)
        return _semaphore


@contextmanager
def rag_concurrency() -> Generator[None, None, None]:
    """获取 RAG 并发许可。超时返回 RAG_BUSY。"""
    sem = get_rag_semaphore()
    acquired = sem.acquire(timeout=RAG_QUEUE_TIMEOUT_SECONDS)
    if not acquired:
        raise RAGBusyError(
            f"系统繁忙，请稍后重试（等待超过 {RAG_QUEUE_TIMEOUT_SECONDS}s）"
        )
    try:
        yield
    finally:
        sem.release()
