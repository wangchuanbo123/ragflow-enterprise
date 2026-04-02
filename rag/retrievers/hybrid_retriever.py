"""
Hybrid检索器
Vector + BM25
"""

from langchain_community.retrievers import BM25Retriever
from langchain.retrievers.ensemble import EnsembleRetriever


def create_hybrid_retriever(vector_db, docs):

    # 向量检索
    vector_retriever = vector_db.as_retriever(
        search_kwargs={"k": 6} # 从向量数据库中检索时返回的相关文档数量，这里设置为 6，表示每次查询将返回与查询最相关的前 6 个文档，这个参数可以根据实际需求进行调整，较大的 k 值可能会增加检索结果的多样性，但也可能引入更多不相关的文档，而较小的 k 值则更专注于最相关的文档，但可能会遗漏一些有用的信息
    )

    # BM25
    bm25 = BM25Retriever.from_documents(docs) # 使用 BM25Retriever 从原始文档列表中创建一个 BM25 检索器实例，BM25 是一种基于词频和逆文档频率的经典文本检索算法，适用于处理文本数据，from_documents 方法会将提供的文档列表转换为适合 BM25 检索器使用的格式，并构建必要的索引结构以支持高效的检索操作
    bm25.k = 6 # 设置 BM25 检索器返回的相关文档数量，这里设置为 6，表示每次查询将返回与查询最相关的前 6 个文档，这个参数可以根据实际需求进行调整，较大的 k 值可能会增加检索结果的多样性，但也可能引入更多不相关的文档，而较小的 k 值则更专注于最相关的文档，但可能会遗漏一些有用的信息

    # Hybrid
    hybrid = EnsembleRetriever(  # EnsembleRetriever 是 LangChain 提供的一个检索器组合器，允许将多个不同类型的检索器组合在一起，并为它们分配权重，以实现更强大的检索能力，retrievers 参数接受一个检索器列表，这些检索器将被组合使用，weights 参数接受一个与检索器列表长度相同的权重列表，这些权重用于控制每个检索器在最终结果中的影响力，较高的权重表示该检索器的结果在最终输出中占更大比例，而较低的权重则表示该检索器的结果占较小比例
        retrievers=[vector_retriever, bm25], # 将向量检索器和 BM25 检索器组合在一起，形成一个混合检索器，这样在查询时
        weights=[0.7, 0.3] #   权重设置为 0.7 和 0.3，表示向量检索器的结果在最终输出中占 70% 的影响力，而 BM25 检索器的结果占 30% 的影响力，这种权重分配可以根据实际需求进行调整，如果你认为向量检索器的结果更相关，可以增加它的权重，反之亦然
    )

    return hybrid