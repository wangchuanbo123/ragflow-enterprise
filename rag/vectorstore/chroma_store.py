"""
向量数据库模块

使用 Chroma Vector DB

Chroma()：简单、本地运行、LangChain原生支持
"""

def load_vector_store(embedding, persist_dir):
    from rag.providers.interfaces import VectorIndexRef
    from rag.providers.vector_store import ChromaVectorStoreProvider

    provider = ChromaVectorStoreProvider(persist_dir=persist_dir)
    return provider.open(embedding, VectorIndexRef())
