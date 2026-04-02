"""
Context 构建节点
作用：
把检索到的 docs 拼接成 context
"""

def context_node(state):
    docs = state["docs"]

    context_parts = []
    sources = []

    for doc in docs:
        content = doc.page_content
        source = doc.metadata.get("source", "unknown")

        context_parts.append(content)

        sources.append({
            "source": source,
            "preview": content[:200]
        })

    context = "\n\n".join(context_parts)

    return {
        "context": context,
        "sources": sources
    }