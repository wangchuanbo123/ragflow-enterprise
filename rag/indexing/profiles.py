"""Stable identities for vector compatibility and document preparation.

No model calls are made here. A changed model artifact must have a distinct
revision (EMBEDDING_REVISION), even when a server keeps the same model alias.
"""
from __future__ import annotations

import hashlib
import json
import os
from importlib.metadata import PackageNotFoundError, version
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from app.core import config


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _endpoint(value) -> str:
    parsed = urlsplit(str(value or ""))
    # Credentials and query parameters never enter persisted identities.
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), host.lower(), parsed.path.rstrip("/"), "", ""))


def embedding_profile(embedding=None) -> dict:
    identity_method = getattr(embedding, "identity", None)
    identity = identity_method() if callable(identity_method) else None
    provider = getattr(identity, "provider", None) or config.EMBEDDING_PROVIDER
    model = getattr(identity, "model", None) or getattr(embedding, "model", None) or config.EMBEDDING_MODEL
    endpoint = getattr(embedding, "base_url", None) or getattr(embedding, "openai_api_base", None)
    if endpoint is None:
        endpoint = config.OLLAMA_BASE_URL if provider == "ollama" else config.EMBEDDING_BASE_URL
    return {
        "format": 1, "provider": provider, "model": model,
        "revision": getattr(identity, "revision", None) or os.getenv("EMBEDDING_REVISION", ""),
        "endpoint": _endpoint(endpoint),
        "document_instruction": getattr(embedding, "embed_instruction", "passage: " if provider == "ollama" else ""),
        "query_instruction": getattr(embedding, "query_instruction", "query: " if provider == "ollama" else ""),
        "dimensions": getattr(embedding, "dimensions", None),
        "normalization": "cosine", "truncate": provider == "ollama",
    }


def embedding_fingerprint(embedding=None) -> str:
    return _digest(embedding_profile(embedding))


@lru_cache(maxsize=1)
def _library_versions() -> dict:
    libraries = {}
    for package in ("pypdf", "docx2txt", "langchain-core", "langchain-text-splitters", "tiktoken"):
        try:
            libraries[package] = version(package)
        except PackageNotFoundError:
            libraries[package] = "unavailable"
    return libraries


def pipeline_profile() -> dict:
    from rag.indexing.tokens import TOKEN_ENCODING_NAME
    from rag.loaders import document_loader as loader

    return {
        "format": 2, "schema": config.INDEX_SCHEMA_VERSION,
        "parser_revision": "native-density-ocr-tables-v2", "libraries": _library_versions(),
        "parser_limits": {name: getattr(loader, name) for name in (
            "MAX_SOURCE_BYTES", "MAX_PARSED_CHARACTERS", "MAX_PDF_PAGES",
            "MAX_DOCX_EXPANDED_BYTES", "MAX_DOCX_PART_BYTES")},
        "ocr": {"languages": loader.PDF_OCR_LANGUAGES, "max_pages": loader.PDF_OCR_MAX_PAGES,
                "max_side": loader.PDF_OCR_MAX_SIDE, "max_page_pixels": loader.PDF_OCR_MAX_PAGE_PIXELS,
                "max_total_pixels": loader.PDF_OCR_MAX_TOTAL_PIXELS, "step_timeout": loader.PDF_OCR_STEP_TIMEOUT_SECONDS,
                "document_timeout": loader.PDF_OCR_DOCUMENT_TIMEOUT_SECONDS, "max_text_bytes": loader.PDF_OCR_MAX_TEXT_BYTES},
        "strategy": config.CHUNK_STRATEGY, "size": config.CHUNK_SIZE,
        "overlap": config.CHUNK_OVERLAP, "minimum": config.MIN_CHUNK_SIZE,
        "tokenizer": TOKEN_ENCODING_NAME,
        "code_min_lines": config.CODE_AWARE_MIN_BLOCK_LINES,
        "images": config.IMAGE_EXTRACTION_ENABLED,
        "max_images": config.MAX_IMAGES_PER_DOCUMENT, "max_image_bytes": config.MAX_IMAGE_BYTES,
    }


def pipeline_fingerprint() -> str:
    return _digest(pipeline_profile())


def index_ref(name, knowledge_base_id, embedding=None):
    from rag.providers.interfaces import VectorIndexRef

    return VectorIndexRef(name=name, knowledge_base_id=knowledge_base_id,
                          embedding_profile_id=embedding_fingerprint(embedding),
                          index_version=pipeline_fingerprint())
