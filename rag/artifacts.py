"""Normalize and deduplicate structured image/code artifacts from RAG sources."""

from __future__ import annotations

import hashlib


def collect_source_artifacts(sources: list[dict] | None) -> tuple[list[dict], list[dict]]:
    images: list[dict] = []
    code_snippets: list[dict] = []
    seen_images: set[str] = set()
    seen_code: set[str] = set()

    for source in sources or []:
        provenance = {
            "knowledge_base_id": source.get("knowledge_base_id", ""),
            "knowledge_base_name": source.get("knowledge_base_name", ""),
            "document_id": source.get("document_id", ""),
            "chunk_id": source.get("chunk_id", ""),
            "citation_id": source.get("citation_id", ""),
        }
        for raw in source.get("images") or []:
            if not isinstance(raw, dict) or not raw.get("url"):
                continue
            item = {**provenance, **raw}
            for name, value in provenance.items():
                if not item.get(name):
                    item[name] = value
            key = str(item["url"])
            if key in seen_images:
                continue
            seen_images.add(key)
            images.append(item)

        for raw in source.get("code_snippets") or []:
            if not isinstance(raw, dict) or not str(raw.get("content") or "").strip():
                continue
            item = {**provenance, **raw}
            for name, value in provenance.items():
                if not item.get(name):
                    item[name] = value
            item.setdefault("path", source.get("source", ""))
            item.setdefault("language", "")
            item.setdefault("start_line", None)
            item.setdefault("end_line", None)
            item.setdefault("line_scope", "chunk")
            item.setdefault("revision", "")
            item.setdefault("origin", "document")
            item.setdefault("is_verbatim", True)
            identity = "\0".join(str(item.get(name) or "") for name in (
                "knowledge_base_id", "document_id", "chunk_id", "path",
                "start_line", "end_line", "content",
            ))
            key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            if key in seen_code:
                continue
            seen_code.add(key)
            code_snippets.append(item)

    return images, code_snippets
