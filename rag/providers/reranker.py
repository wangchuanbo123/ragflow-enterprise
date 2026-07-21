from functools import cached_property
from typing import List, Sequence

from langchain_core.documents import Document
from sentence_transformers import CrossEncoder


class BgeReranker:
    def __init__(self, model: str, default_top_k: int = 5):
        self.model = model
        self.default_top_k = default_top_k

    @cached_property
    def _model(self):
        return CrossEncoder(self.model)

    def rerank(
        self,
        query: str,
        docs: Sequence[Document],
        top_k: int | None = None,
    ) -> List[Document]:
        if not docs:
            return []

        pairs = [(query, doc.page_content) for doc in docs]
        scores = self._model.predict(pairs)
        ranked = sorted(zip(docs, scores), key=lambda item: item[1], reverse=True)
        limit = self.default_top_k if top_k is None else top_k
        return [doc for doc, _ in ranked[:limit]]


class PassthroughReranker:
    def __init__(self, default_top_k: int = 5):
        self.default_top_k = default_top_k

    def rerank(
        self,
        query: str,
        docs: Sequence[Document],
        top_k: int | None = None,
    ) -> List[Document]:
        del query
        limit = self.default_top_k if top_k is None else top_k
        return list(docs[:limit])
