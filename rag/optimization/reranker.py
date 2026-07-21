"""
Reranker模块

使用 cross-encoder 模型

向量检索其实是 粗筛，得到的结果可能包含一些不相关的文档。Reranker 模块的作用是对这些候选文档进行更精细的排序，以提升最终返回结果的相关性和质量。
"""

def rerank(query, docs):
    from rag.providers.factory import get_reranker

    return get_reranker().rerank(query, docs)
