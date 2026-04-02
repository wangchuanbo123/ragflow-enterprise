from langgraph.graph import StateGraph, END
from rag.state.rag_state import RAGState

from rag.nodes.context_node import context_node
from rag.nodes.rewrite_node import rewrite_node
from rag.nodes.retrieve_node import retrieve_node
from rag.nodes.rerank_node import rerank_node
from rag.nodes.generate_node import generate_node


def build_graph():

    workflow = StateGraph(RAGState) 

    # 节点
    workflow.add_node("rewrite", rewrite_node)
    workflow.add_node("retrieve", retrieve_node)
    workflow.add_node("rerank", rerank_node)
    workflow.add_node("build_context", context_node)
    workflow.add_node("generate", generate_node)

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