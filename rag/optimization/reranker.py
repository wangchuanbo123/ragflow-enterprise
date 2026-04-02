"""
Reranker模块

使用 cross-encoder 模型

向量检索其实是 粗筛，得到的结果可能包含一些不相关的文档。Reranker 模块的作用是对这些候选文档进行更精细的排序，以提升最终返回结果的相关性和质量。
"""

from sentence_transformers import CrossEncoder
# 来自 sentence-transformers


model = CrossEncoder("BAAI/bge-reranker-base")  # 选择一个适合 reranking 的 cross-encoder 模型，这里使用了 BAAI 的 bge-reranker-base 模型，可以根据需要替换为其他模型


def rerank(query, docs):

    pairs = [(query, d.page_content) for d in docs]  # 构建 query-document 对，供 cross-encoder 评分使用。这里假设文档对象有 page_content 属性存储文本内容，根据实际情况调整属性名称

    scores = model.predict(pairs)  # 使用 cross-encoder 模型对 query-document 对进行评分，得到相关性分数

    ranked = sorted(
        zip(docs, scores),  # 将文档和对应的分数打包成元组列表
        key=lambda x: x[1],  # 根据分数进行排序，x[1] 是分数
        reverse=True  # 降序排序，分数高的排在前面
    )

    return [doc for doc, score in ranked[:5]]  # 返回排序后的前5个文档，可以根据需要调整返回数量