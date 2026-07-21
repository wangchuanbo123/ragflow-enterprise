from functools import cached_property

from langchain_community.embeddings import OllamaEmbeddings


class OllamaEmbeddingProvider:
    def __init__(self, model: str, base_url: str):
        self.model = model
        self.base_url = base_url

    @cached_property
    def _model(self):
        return OllamaEmbeddings(model=self.model, base_url=self.base_url)

    def get_model(self):
        return self._model
