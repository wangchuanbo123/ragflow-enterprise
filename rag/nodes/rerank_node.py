"""
Rerank Node
使用 bge-reranker 对检索结果重新排序
"""

from sentence_transformers import CrossEncoder


# 加载 reranker 模型
reranker = CrossEncoder("BAAI/bge-reranker-base")


def rerank_node(state):
    """
    输入: docs
    输出: docs (reranked)
    """

    query = state["query"]
    docs = state["docs"]

    pairs = []
    for doc in docs:
        pairs.append((query, doc.page_content))  # 构建 query-document 对，供 cross-encoder 评分使用。这里假设文档对象有 page_content 属性存储文本内容，根据实际情况调整属性名称

    scores = reranker.predict(pairs) #  使用 cross-encoder 对 query-document 对进行评分，得到一个分数列表，分数越高表示文档与查询的相关性越强

    ranked = sorted(  # 将文档和对应的分数打包成元组列表，并根据分数进行排序，x[1] 是分数，降序排序，分数高的排在前面
        zip(docs, scores),  # 输入数据：[(doc1, score1), (doc2, score2), ...]
        key=lambda x: x[1], # 排序依据：每个元组的第二个元素（分数）
        reverse=True #  设置 reverse=True 表示降序排序
    )

    reranked_docs = [doc for doc, score in ranked[:5]]  # 返回排序后的前5个文档，可以根据需要调整返回数量

    return {
        "docs": reranked_docs
    }