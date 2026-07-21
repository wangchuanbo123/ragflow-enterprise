from langgraph.graph import StateGraph, END
from rag.state.rag_state import RAGState

from rag.nodes.context_node import context_node
from rag.nodes.rewrite_node import create_rewrite_node
from rag.nodes.retrieve_node import create_retrieve_node
from rag.nodes.rerank_node import create_rerank_node
from rag.nodes.generate_node import create_generate_node
from rag.runtime.runtime import RAGRuntime, get_runtime


def build_graph(runtime: RAGRuntime | None = None):

    runtime = runtime or get_runtime()

    workflow = StateGraph(RAGState) 

    # 节点
    workflow.add_node("rewrite", create_rewrite_node(runtime.llm))
    workflow.add_node("retrieve", create_retrieve_node(runtime.retriever))
    workflow.add_node("rerank", create_rerank_node(runtime.reranker))
    workflow.add_node("build_context", context_node)
    workflow.add_node("generate", create_generate_node(runtime.llm))

    # 入口
    workflow.set_entry_point("rewrite")

    # 流程
    workflow.add_edge("rewrite", "retrieve")
    workflow.add_edge("retrieve", "rerank")
    workflow.add_edge("rerank", "build_context")
    workflow.add_edge("build_context", "generate")

    # 结束
    workflow.add_edge("generate", END)

    return workflow.compile()
