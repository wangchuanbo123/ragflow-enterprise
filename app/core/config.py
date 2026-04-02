# config.py

"""
项目配置模块

使用 Pathlib 处理路径
原因：
- 兼容 Windows / Linux
- 自动处理路径分隔符
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # 获取项目根目录，回退三层

DATA_DIR = BASE_DIR / "data"  # 等价rag_langgraph_enterprise/data

DOC_DIR = DATA_DIR / "docs"

VECTOR_DB_DIR = DATA_DIR / "vector_db"  # Chroma 存储位置

LLM_MODEL = "deepseek-coder:1.3b" # LLM模型

EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # embedding模型