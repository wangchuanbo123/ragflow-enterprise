from langchain_core.documents import Document

from rag.retrievers.hybrid_retriever import HybridRRFRetriever


class RecordingVectorStore:
    def __init__(self):
        self.filters = []

    def similarity_search_with_relevance_scores(self, query, k, **kwargs):
        del query, k
        self.filters.append(kwargs.get("filter"))
        return [
            (Document(page_content="one", metadata={"chunk_id": "c1", "document_id": "doc-1"}), 0.9),
            (Document(page_content="two", metadata={"chunk_id": "c2", "document_id": "doc-2"}), 0.8),
        ]


class RecordingBM25:
    def __init__(self):
        self.allowed = None

    def get_relevant_documents(self, query, top_k, allowed_document_ids=None):
        del query, top_k
        self.allowed = allowed_document_ids
        return [
            Document(page_content="two", metadata={"chunk_id": "c2", "document_id": "doc-2"}),
        ]


def test_scope_is_pushed_into_vector_and_bm25(monkeypatch):
    vector = RecordingVectorStore()
    bm25 = RecordingBM25()
    monkeypatch.setattr(
        "rag.retrievers.hybrid_retriever.get_bm25_retriever",
        lambda knowledge_base_id: bm25,
    )
    retriever = HybridRRFRetriever(vector, knowledge_base_id="kb-1")

    documents = retriever.retrieve_single("query", allowed_document_ids={"doc-2"})

    assert vector.filters == [{"document_id": {"$in": ["doc-2"]}}]
    assert bm25.allowed == {"doc-2"}
    assert {document.metadata["document_id"] for document in documents} == {"doc-2"}
