"""Compiled, route-aware LangGraph used by every question-answering entry point."""

from langgraph.graph import END, StateGraph

from rag.nodes.channel_retrieval_nodes import (
    create_code_retrieve_node,
    create_graph_retrieve_node,
    create_text_retrieve_node,
    fusion_node,
)
from rag.nodes.context_node import context_node
from rag.nodes.embed_query_node import create_embed_query_node
from rag.nodes.generate_node import create_generate_node
from rag.nodes.rerank_node import create_rerank_node
from rag.nodes.rewrite_node import create_rewrite_node
from rag.nodes.route_node import create_post_rewrite_route_node, create_route_node
from rag.runtime.runtime import RAGRuntime, get_current_runtime
from rag.state.rag_state import RAGState


def _after_context(state: RAGState) -> str:
    return "end" if state.get("generation_mode") == "context" else "generate"


def build_graph(runtime: RAGRuntime | None = None):
    """Build one workflow for sync answers and streaming context preparation.

    Streaming requests stop after context construction and stream the model using
    the same prepared state. Synchronous requests continue to the generate node.
    """
    runtime = runtime or get_current_runtime()
    workflow = StateGraph(RAGState)

    workflow.add_node(
        "rewrite",
        create_rewrite_node(runtime.rewrite_llm or runtime.llm),
    )
    workflow.add_node("route_request", create_route_node(runtime))
    workflow.add_node(
        "route_after_rewrite",
        create_post_rewrite_route_node(runtime),
    )
    workflow.add_node(
        "embed_query",
        create_embed_query_node(runtime.embedding_model),
    )
    workflow.add_node("text_retrieve", create_text_retrieve_node(runtime.retriever))
    workflow.add_node(
        "graph_retrieve",
        create_graph_retrieve_node(runtime.graph_retriever),
    )
    workflow.add_node(
        "code_retrieve",
        create_code_retrieve_node(runtime.code_retriever),
    )
    workflow.add_node("fuse", fusion_node)
    workflow.add_node("rerank", create_rerank_node(runtime.reranker))
    workflow.add_node("build_context", context_node)
    workflow.add_node("generate", create_generate_node(runtime.llm))

    workflow.set_entry_point("route_request")
    workflow.add_edge("route_request", "rewrite")
    workflow.add_edge("rewrite", "route_after_rewrite")
    workflow.add_edge("route_after_rewrite", "embed_query")
    # Independent channels fan out concurrently. Each writes distinct state
    # keys, and the list edge acts as the fan-in barrier before fusion.
    workflow.add_edge("embed_query", "text_retrieve")
    workflow.add_edge("embed_query", "graph_retrieve")
    workflow.add_edge("embed_query", "code_retrieve")
    workflow.add_edge(
        ["text_retrieve", "graph_retrieve", "code_retrieve"],
        "fuse",
    )
    workflow.add_edge("fuse", "rerank")
    workflow.add_edge("rerank", "build_context")
    workflow.add_conditional_edges(
        "build_context",
        _after_context,
        {"generate": "generate", "end": END},
    )
    workflow.add_edge("generate", END)

    return workflow.compile()
