"""
Retrieve Node
"""

from rag.runtime.runtime import retriever

def retrieve_node(state):
    query = state["query"]

    results = retriever.get_relevant_documents(query)

    return {
        "docs": results
    }