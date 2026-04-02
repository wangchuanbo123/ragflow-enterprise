"""
RAG Service

提供统一调用接口

API调用入口
"""

from rag.graph.rag_graph import build_graph

graph = build_graph()

def ask_question(query):

    result = graph.invoke({
        "query": query
    })

    answer = result["answer"]
    sources = result.get("sources", [])

    print("\n==== RAG Sources ====")

    for s in sources:
        print("文件:", s["source"])
        print("内容片段:", s["preview"])
        print("------")

    return {
        "answer": answer,
        "sources": sources
    }