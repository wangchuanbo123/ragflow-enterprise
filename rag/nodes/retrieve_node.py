"""Retrieve node."""

from rag.providers.interfaces import Retriever


def create_retrieve_node(retriever: Retriever):
    def retrieve_node(state):
        return {
            "docs": retriever.get_relevant_documents(state["query"]),
        }

    return retrieve_node


def retrieve_node(state):
    from rag.runtime.runtime import get_runtime

    return create_retrieve_node(get_runtime().retriever)(state)
