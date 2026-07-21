# config.py

"""
项目配置模块

使用 Pathlib 处理路径
原因：
- 兼容 Windows / Linux
- 自动处理路径分隔符
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # 获取项目根目录，回退三层

load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"  # 等价rag_langgraph_enterprise/data

DOC_DIR = DATA_DIR / "docs"

VECTOR_DB_DIR = DATA_DIR / "vector_db"  # Chroma 存储位置

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "zhipu").lower()

LLM_MODEL = os.getenv("LLM_MODEL", "glm-5.2")

LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("ZHIPU_API_KEY", "")

LLM_BASE_URL = (
    os.getenv("LLM_BASE_URL")
    or os.getenv("ZHIPU_BASE_URL")
    or ""
)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen3:4b")

EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "ollama").lower()
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text-v2-moe")

RERANKER_PROVIDER = os.getenv("RERANKER_PROVIDER", "bge").lower()
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-base")
RERANK_TOP_K = int(os.getenv("RERANK_TOP_K", "5"))

VECTOR_STORE_PROVIDER = os.getenv("VECTOR_STORE_PROVIDER", "chroma").lower()

VECTOR_SEARCH_K = int(os.getenv("VECTOR_SEARCH_K", "6"))
BM25_SEARCH_K = int(os.getenv("BM25_SEARCH_K", "6"))
HYBRID_VECTOR_WEIGHT = float(os.getenv("HYBRID_VECTOR_WEIGHT", "0.7"))
