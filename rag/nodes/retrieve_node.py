"""Retrieve node. 使用原始+改写问题进行双查询检索与 RRF 融合。"""

import logging

from rag.providers.interfaces import Retriever
from rag.retrievers.hybrid_graph_retriever import HybridGraphRetriever
from rag.retrievers.hybrid_retriever import HybridRRFRetriever

logger = logging.getLogger(__name__)


def create_retrieve_node(retriever: Retriever):
    def retrieve_node(state):
        queries = state.get("retrieval_queries")
        if not queries:
            original = state.get("original_query") or state.get("query", "")
            retrieval_query = state.get("retrieval_query") or original
            queries = [retrieval_query] if retrieval_query else [original]

        # Deduplicate queries
        seen = set()
        unique_queries = []
        for q in queries:
            norm = q.strip()
            if norm and norm not in seen:
                seen.add(norm)
                unique_queries.append(norm)

        if not unique_queries:
            return {"docs": [], "retrieval_candidates": []}

        if isinstance(retriever, (HybridRRFRetriever, HybridGraphRetriever)):
            if len(unique_queries) > 1:
                docs = retriever.retrieve_multi_query(unique_queries)
            else:
                docs = retriever.retrieve_single(unique_queries[0])
        else:
            # Generic retriever: query with each unique query and deduplicate by chunk_id
            all_docs = []
            seen_ids = set()
            for q in unique_queries:
                for doc in retriever.get_relevant_documents(q):
                    cid = doc.metadata.get("chunk_id", id(doc))
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        all_docs.append(doc)
            docs = all_docs

        return {
            "docs": docs,
            "retrieval_candidates": docs,
        }

    return retrieve_node


def retrieve_node(state):
    from rag.runtime.runtime import get_runtime
    return create_retrieve_node(get_runtime().retriever)(state)
