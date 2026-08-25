"""Create one semantic query vector and share it across retrieval channels."""

from __future__ import annotations

from time import perf_counter


def create_embed_query_node(embedding_model):
    def embed_query_node(state):
        started = perf_counter()
        query = (
            state.get("retrieval_query")
            or state.get("original_query")
            or state.get("query", "")
        )
        route = dict(state.get("route") or {})
        vector = None
        status = "skipped"
        error = None
        if embedding_model is not None and query:
            try:
                vector = list(embedding_model.embed_query(query))
                status = "ok"
            except Exception as exc:
                status = "error"
                error = f"{type(exc).__name__}: {exc}"[:500]
        embedding_info = {
            "status": status,
            "shared": vector is not None,
            "dimensions": len(vector or []),
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
        }
        if error:
            embedding_info["error"] = error
        route["query_embedding"] = embedding_info
        return {"query_embedding": vector, "route": route}

    return embed_query_node

