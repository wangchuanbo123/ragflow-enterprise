"""
Embedding模型加载模块

使用 LangChain 的 embedding wrapper

这个是LangChain统一 embedding 接口

可替换 OpenAI / BGE / Instructor
"""

from langchain_community.embeddings import SentenceTransformerEmbeddings
# ↑ 来自 LangChain Community
# 作用：加载 SentenceTransformer embedding 模型

from app.core.config import EMBEDDING_MODEL


def get_embedding_model():

    embeddings = SentenceTransformerEmbeddings(
        model_name=EMBEDDING_MODEL
    )

    return embeddings