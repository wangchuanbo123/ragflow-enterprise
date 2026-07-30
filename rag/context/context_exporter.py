"""Export the final RAG context for local debugging."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def export_context_snapshot(
    state: Mapping[str, Any],
    *,
    enabled: bool | None = None,
    output_dir: str | Path | None = None,
) -> Path | None:
    """Write the exact answer context and its sources to a JSON snapshot.

    Export failures are logged and do not interrupt answer generation.
    """
    if enabled is None or output_dir is None:
        from app.core.config import (
            CONTEXT_JSON_EXPORT_DIR,
            CONTEXT_JSON_EXPORT_ENABLED,
        )

        if enabled is None:
            enabled = CONTEXT_JSON_EXPORT_ENABLED
        if output_dir is None:
            output_dir = CONTEXT_JSON_EXPORT_DIR

    if not enabled:
        return None

    context = str(state.get("context") or "")
    sources = list(state.get("sources") or [])
    original_query = str(state.get("original_query") or state.get("query") or "")
    retrieval_query = str(state.get("retrieval_query") or original_query)
    retrieval_queries = list(state.get("retrieval_queries") or [retrieval_query])

    graph_facts = [
        {
            "citation_id": source.get("citation_id", ""),
            "document_id": source.get("document_id", ""),
            "chunk_id": source.get("chunk_id", ""),
            "facts": source.get("graph_facts", []),
        }
        for source in sources
        if source.get("graph_facts")
    ]

    payload = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "original_query": original_query,
        "retrieval_query": retrieval_query,
        "retrieval_queries": retrieval_queries,
        "llm_input": {
            "query": original_query,
            "context": context,
        },
        "context_character_count": len(context),
        "sources": sources,
        "graph_facts": graph_facts,
    }

    directory = Path(output_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"context_{timestamp}_{uuid.uuid4().hex[:8]}.json"
    temporary = target.with_suffix(".tmp")

    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    except OSError as exc:
        logger.warning("Failed to export context JSON to %s: %s", directory, exc)
        return None

    logger.info("Context JSON exported to %s", target)
    return target
