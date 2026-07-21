"""
向量数据库模块

使用 Chroma Vector DB

Chroma()：简单、本地运行、LangChain原生支持
"""

def load_vector_store(embedding, persist_dir):
    from rag.providers.factory import get_vector_store_provider

    return get_vector_store_provider().load(embedding, persist_dir)
