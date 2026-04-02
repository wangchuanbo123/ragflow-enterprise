"""
向量数据库模块

使用 Chroma Vector DB

Chroma()：简单、本地运行、LangChain原生支持
"""

from langchain_community.vectorstores import Chroma
# 来自 LangChain Community
# wrapper for Chroma vector database


def load_vector_store(embedding, persist_dir):

    db = Chroma( # 创建一个 Chroma 向量数据库实例，embedding 参数指定了用于计算向量的 embedding 模型，persist_directory 参数指定了数据库的存储位置（本地文件夹路径），如果该目录不存在，Chroma 会自动创建一个新的数据库并将数据存储在其中
        persist_directory=persist_dir, # 指定 Chroma 数据库的存储目录，这个目录将用于存储向量数据和相关的元数据，Chroma 会在这个目录下创建必要的文件和子目录来管理数据库
        embedding_function=embedding
    )

    return db