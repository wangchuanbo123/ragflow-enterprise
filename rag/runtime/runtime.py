"""
RAG Runtime
系统启动时初始化所有重资源
"""

from rag.embeddings.embedding_model import get_embedding_model
from rag.vectorstore.chroma_store import load_vector_store
from rag.retrievers.hybrid_retriever import create_hybrid_retriever
from rag.loaders.document_loader import load_documents

from app.core.config import VECTOR_DB_DIR, DOC_DIR


print("初始化 RAG 系统...")

# 1 embedding
embedding = get_embedding_model()

# 2 vector db
vector_db = load_vector_store(
    embedding=embedding,
    persist_dir=str(VECTOR_DB_DIR)
)

# 3 docs
docs = load_documents(str(DOC_DIR))

# 4 hybrid retriever
retriever = create_hybrid_retriever(vector_db, docs)

print("RAG 初始化完成")