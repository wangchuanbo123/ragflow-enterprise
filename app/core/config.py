# config.py

"""
项目配置模块

使用 Pathlib 处理路径
原因：
- 兼容 Windows / Linux
- 自动处理路径分隔符
"""

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # 获取项目根目录，回退三层

ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)

# Chroma telemetry is not needed by this local/offline-first application.
# Set this before any Chroma client is created.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")


def _ensure_local_secret(key: str, env_path: Path = ENV_PATH) -> str:
    """确保本地 .env 中存在指定密钥；缺失时生成强随机值并写回。

    只补充缺失配置，绝不覆盖或删除已有内容。
    """
    current = os.getenv(key, "").strip()
    if current:
        return current

    generated = secrets.token_urlsafe(48)
    lines = []
    found = False
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{key}="):
                    lines.append(f"{key}={generated}\n")
                    found = True
                else:
                    lines.append(line)

    if not found:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(f"{key}={generated}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    os.environ[key] = generated
    return generated

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

VECTOR_SEARCH_K = int(os.getenv("VECTOR_SEARCH_K", "8"))
BM25_SEARCH_K = int(os.getenv("BM25_SEARCH_K", "8"))
HYBRID_VECTOR_WEIGHT = float(os.getenv("HYBRID_VECTOR_WEIGHT", "0.70"))

# --- Index and chunking ---
INDEX_SCHEMA_VERSION = int(os.getenv("INDEX_SCHEMA_VERSION", "2"))
CHUNK_STRATEGY = os.getenv("CHUNK_STRATEGY", "recursive").lower()
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "384"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "64"))
MIN_CHUNK_SIZE = int(os.getenv("MIN_CHUNK_SIZE", "80"))
INGEST_BATCH_SIZE = int(os.getenv("INGEST_BATCH_SIZE", "128"))
MAX_DOCUMENT_SIZE_MB = int(os.getenv("MAX_DOCUMENT_SIZE_MB", "50"))
INDEX_WORKER_ENABLED = os.getenv("INDEX_WORKER_ENABLED", "true").lower() == "true"
INDEX_MAX_CONCURRENCY = int(os.getenv("INDEX_MAX_CONCURRENCY", "1"))

# --- Retrieval fusion ---
RRF_K = int(os.getenv("RRF_K", "60"))
RETRIEVAL_CANDIDATE_K = int(os.getenv("RETRIEVAL_CANDIDATE_K", "12"))

# --- Context budget ---
MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", "3000"))
MAX_CONTEXT_CHUNKS = int(os.getenv("MAX_CONTEXT_CHUNKS", "5"))
MAX_CHUNKS_PER_SOURCE = int(os.getenv("MAX_CHUNKS_PER_SOURCE", "2"))
CONTEXT_TOKEN_SAFETY_RATIO = float(os.getenv("CONTEXT_TOKEN_SAFETY_RATIO", "0.85"))
CONTEXT_JSON_EXPORT_ENABLED = os.getenv("CONTEXT_JSON_EXPORT_ENABLED", "false").lower() == "true"
_context_json_export_dir = Path(
    os.getenv("CONTEXT_JSON_EXPORT_DIR", "data/debug/contexts")
)
CONTEXT_JSON_EXPORT_DIR = (
    _context_json_export_dir
    if _context_json_export_dir.is_absolute()
    else BASE_DIR / _context_json_export_dir
)

# --- Runtime reliability ---
RAG_MAX_CONCURRENCY = int(os.getenv("RAG_MAX_CONCURRENCY", "2"))
RAG_QUEUE_TIMEOUT_SECONDS = int(os.getenv("RAG_QUEUE_TIMEOUT_SECONDS", "3"))
PROVIDER_TIMEOUT_SECONDS = int(os.getenv("PROVIDER_TIMEOUT_SECONDS", "120"))
READINESS_TIMEOUT_SECONDS = int(os.getenv("READINESS_TIMEOUT_SECONDS", "2"))
READINESS_CACHE_SECONDS = int(os.getenv("READINESS_CACHE_SECONDS", "10"))

# --- Knowledge graph ---
KNOWLEDGE_GRAPH_ENABLED = os.getenv("KNOWLEDGE_GRAPH_ENABLED", "true").lower() == "true"
GRAPH_STORE_PROVIDER = os.getenv("GRAPH_STORE_PROVIDER", "sqlite").lower()
GRAPH_EXTRACTION_PROVIDER = os.getenv("GRAPH_EXTRACTION_PROVIDER", "current").lower()
GRAPH_EXTRACTION_MODEL = os.getenv("GRAPH_EXTRACTION_MODEL", LLM_MODEL)
GRAPH_SCHEMA_VERSION = int(os.getenv("GRAPH_SCHEMA_VERSION", "1"))
GRAPH_EXTRACTION_BATCH_SIZE = int(os.getenv("GRAPH_EXTRACTION_BATCH_SIZE", "6"))
GRAPH_EXTRACTION_MAX_CHARS = int(os.getenv("GRAPH_EXTRACTION_MAX_CHARS", "6000"))
GRAPH_EXTRACTION_MAX_RETRIES = int(os.getenv("GRAPH_EXTRACTION_MAX_RETRIES", "1"))
GRAPH_MIN_CONFIDENCE = float(os.getenv("GRAPH_MIN_CONFIDENCE", "0.60"))
GRAPH_ENTITY_SEARCH_K = int(os.getenv("GRAPH_ENTITY_SEARCH_K", "5"))
GRAPH_MAX_HOPS = int(os.getenv("GRAPH_MAX_HOPS", "2"))
GRAPH_MAX_RELATIONS = int(os.getenv("GRAPH_MAX_RELATIONS", "50"))

# --- Three-way retrieval ---
VECTOR_RETRIEVAL_WEIGHT = float(os.getenv("VECTOR_RETRIEVAL_WEIGHT", "0.50"))
BM25_RETRIEVAL_WEIGHT = float(os.getenv("BM25_RETRIEVAL_WEIGHT", "0.25"))
GRAPH_RETRIEVAL_WEIGHT = float(os.getenv("GRAPH_RETRIEVAL_WEIGHT", "0.25"))

# Validate three-way weights
_total_w = VECTOR_RETRIEVAL_WEIGHT + BM25_RETRIEVAL_WEIGHT + GRAPH_RETRIEVAL_WEIGHT
if _total_w <= 0:
    raise ValueError(f"Retrieval weights must sum > 0, got {_total_w}")
if GRAPH_MAX_HOPS not in (1, 2):
    raise ValueError(f"GRAPH_MAX_HOPS must be 1 or 2, got {GRAPH_MAX_HOPS}")
if not (0.0 <= GRAPH_MIN_CONFIDENCE <= 1.0):
    raise ValueError(
        "GRAPH_MIN_CONFIDENCE must be between 0 and 1, "
        f"got {GRAPH_MIN_CONFIDENCE}"
    )
if GRAPH_EXTRACTION_BATCH_SIZE < 1:
    raise ValueError("GRAPH_EXTRACTION_BATCH_SIZE must be at least 1")
if GRAPH_EXTRACTION_MAX_CHARS < 100:
    raise ValueError("GRAPH_EXTRACTION_MAX_CHARS must be at least 100")

# --- Config validation ---
if CHUNK_OVERLAP >= CHUNK_SIZE:
    raise ValueError(f"CHUNK_OVERLAP ({CHUNK_OVERLAP}) must be less than CHUNK_SIZE ({CHUNK_SIZE})")
if not (0.0 <= HYBRID_VECTOR_WEIGHT <= 1.0):
    raise ValueError(f"HYBRID_VECTOR_WEIGHT must be between 0 and 1, got {HYBRID_VECTOR_WEIGHT}")
if CHUNK_SIZE < 50:
    raise ValueError(f"CHUNK_SIZE must be >= 50, got {CHUNK_SIZE}")
if RRF_K < 1:
    raise ValueError(f"RRF_K must be >= 1, got {RRF_K}")
if not (0.0 < CONTEXT_TOKEN_SAFETY_RATIO <= 1.0):
    raise ValueError(f"CONTEXT_TOKEN_SAFETY_RATIO must be in (0, 1], got {CONTEXT_TOKEN_SAFETY_RATIO}")
if RAG_MAX_CONCURRENCY < 1:
    raise ValueError("RAG_MAX_CONCURRENCY must be at least 1")
if RAG_QUEUE_TIMEOUT_SECONDS < 0:
    raise ValueError("RAG_QUEUE_TIMEOUT_SECONDS cannot be negative")
if PROVIDER_TIMEOUT_SECONDS <= 0:
    raise ValueError("PROVIDER_TIMEOUT_SECONDS must be greater than 0")

APP_ENV = os.getenv("APP_ENV", "development").lower()
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'app.db'}")
JWT_SECRET_KEY = _ensure_local_secret("JWT_SECRET_KEY")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "480"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
    if origin.strip()
]
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "10"))
MAX_MESSAGE_LENGTH = int(os.getenv("MAX_MESSAGE_LENGTH", "4000"))
INITIAL_ADMIN_USERNAME = os.getenv("INITIAL_ADMIN_USERNAME", "admin")
INITIAL_ADMIN_PASSWORD = _ensure_local_secret("INITIAL_ADMIN_PASSWORD")
JWT_COOKIE_NAME = "rag_auth_token"
