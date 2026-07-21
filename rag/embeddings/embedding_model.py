from langchain_community.embeddings import OllamaEmbeddings

from app.core.config import EMBEDDING_MODEL


def get_embedding_model():
    return OllamaEmbeddings(model=EMBEDDING_MODEL)
