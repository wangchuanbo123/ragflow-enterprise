def get_embedding_model():
    from rag.providers.factory import get_embedding_provider

    return get_embedding_provider().get_model()
