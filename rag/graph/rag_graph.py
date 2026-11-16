"""Application-owned deterministic RAG workflow, independent of agent frameworks.

Knowledge work uses a pluggable agent backend. Online question answering keeps
bounded routing/retrieval stages and exposes the existing ``invoke`` API.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from rag.nodes.channel_retrieval_nodes import (
    create_code_retrieve_node, create_graph_retrieve_node,
    create_text_retrieve_node, fusion_node,
)
from rag.nodes.context_node import context_node
from rag.nodes.embed_query_node import create_embed_query_node
from rag.nodes.generate_node import create_generate_node
from rag.nodes.rerank_node import create_rerank_node
from rag.nodes.rewrite_node import create_rewrite_node
from rag.nodes.route_node import create_post_rewrite_route_node, create_route_node
from rag.runtime.runtime import RAGRuntime, get_current_runtime
from rag.state.rag_state import RAGState


class RAGWorkflow:
    """A reusable, stateless pipeline; each invocation owns its mutable state."""

    def __init__(self, runtime: RAGRuntime):
        self.before_retrieval = (
            create_route_node(runtime),
            create_rewrite_node(runtime.rewrite_llm or runtime.llm),
            create_post_rewrite_route_node(runtime),
            create_embed_query_node(runtime.embedding_model),
        )
        self.retrieval_channels = (
            create_text_retrieve_node(runtime.retriever),
            create_graph_retrieve_node(runtime.graph_retriever),
            create_code_retrieve_node(runtime.code_retriever),
        )
        self.after_retrieval = (
            fusion_node, create_rerank_node(runtime.reranker), context_node,
        )
        self.generate = create_generate_node(runtime.llm)

    @staticmethod
    def _merge(state: dict, updates: dict) -> None:
        for key, value in updates.items():
            if key == "parallel_markers":
                state[key] = list(state.get(key) or []) + list(value or [])
            else:
                state[key] = value

    def invoke(self, initial_state: RAGState, config=None, **kwargs) -> RAGState:
        del config, kwargs
        state = deepcopy(dict(initial_state))
        for node in self.before_retrieval:
            self._merge(state, node(state))
        # Branches read independent snapshots. Fixed merge order makes fusion
        # and citation numbering independent of thread scheduling.
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="rag-retrieval") as executor:
            futures = [executor.submit(node, deepcopy(state)) for node in self.retrieval_channels]
            updates = [future.result() for future in futures]
        seen_keys = set()
        for update in updates:
            overlapping = (set(update) - {"parallel_markers"}) & seen_keys
            if overlapping:
                raise ValueError(f"Retrieval channels wrote overlapping state keys: {sorted(overlapping)}")
            seen_keys.update(set(update) - {"parallel_markers"})
            self._merge(state, update)
        for node in self.after_retrieval:
            self._merge(state, node(state))
        if state.get("generation_mode") != "context":
            self._merge(state, self.generate(state))
        return state


def build_graph(runtime: RAGRuntime | None = None) -> RAGWorkflow:
    """Compatibility entry point for the framework-independent RAG runner."""
    return RAGWorkflow(runtime or get_current_runtime())
