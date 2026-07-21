"""
Hybrid检索器
Vector + BM25
"""

from langchain_community.retrievers import BM25Retriever
from langchain.retrievers.ensemble import EnsembleRetriever

from app.core.config import BM25_SEARCH_K, HYBRID_VECTOR_WEIGHT, VECTOR_SEARCH_K


def create_hybrid_retriever(
    vector_db,
    docs,
    vector_k=VECTOR_SEARCH_K,
    bm25_k=BM25_SEARCH_K,
    vector_weight=HYBRID_VECTOR_WEIGHT,
):

    # 向量检索
    vector_retriever = vector_db.as_retriever(
        search_kwargs={"k": vector_k}
    )

    # BM25
    bm25 = BM25Retriever.from_documents(docs) # 使用 BM25Retriever 从原始文档列表中创建一个 BM25 检索器实例，BM25 是一种基于词频和逆文档频率的经典文本检索算法，适用于处理文本数据，from_documents 方法会将提供的文档列表转换为适合 BM25 检索器使用的格式，并构建必要的索引结构以支持高效的检索操作
    bm25.k = bm25_k

    # Hybrid
    hybrid = EnsembleRetriever(  # EnsembleRetriever 是 LangChain 提供的一个检索器组合器，允许将多个不同类型的检索器组合在一起，并为它们分配权重，以实现更强大的检索能力，retrievers 参数接受一个检索器列表，这些检索器将被组合使用，weights 参数接受一个与检索器列表长度相同的权重列表，这些权重用于控制每个检索器在最终结果中的影响力，较高的权重表示该检索器的结果在最终输出中占更大比例，而较低的权重则表示该检索器的结果占较小比例
        retrievers=[vector_retriever, bm25],
        weights=[vector_weight, 1 - vector_weight]
    )

    return hybrid
