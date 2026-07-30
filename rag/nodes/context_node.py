"""
Context 构建节点
调用 ContextBuilder 构建上下文与引用。
"""

from rag.context.context_builder import build_context
from rag.context.context_exporter import export_context_snapshot


def context_node(state):
    docs = state.get("docs") or []
    context, sources = build_context(docs)

    result = {
        "context": context,
        "sources": sources,
    }
    snapshot = export_context_snapshot({**state, **result})
    if snapshot is not None:
        result["context_json_file"] = str(snapshot)
    return result
