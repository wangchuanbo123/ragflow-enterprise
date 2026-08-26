# config.py

"""
项目配置模块

使用 Pathlib 处理路径
原因：
- 兼容 Windows / Linux
- 自动处理路径分隔符
"""

import os
import re
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # 获取项目根目录，回退三层

ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value, got {raw!r}")

APP_PROFILE = os.getenv("APP_PROFILE", "local").strip().lower()
if APP_PROFILE not in {"local", "test", "enterprise"}:
    raise ValueError(
        "APP_PROFILE must be one of local, test, enterprise; "
        f"got {APP_PROFILE!r}"
    )

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

    if APP_PROFILE == "enterprise":
        raise ValueError(
            f"{key} must be supplied by the enterprise secret provider; "
            "automatic .env writes are disabled"
        )

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

# Purpose-specific model profiles. Legacy LLM_* values remain the defaults so
# existing local deployments keep exactly the same behavior.
ANSWER_LLM_PROVIDER = (os.getenv("ANSWER_LLM_PROVIDER") or LLM_PROVIDER).lower()
ANSWER_LLM_MODEL = os.getenv("ANSWER_LLM_MODEL") or LLM_MODEL
ANSWER_LLM_API_KEY = os.getenv("ANSWER_LLM_API_KEY") or LLM_API_KEY
ANSWER_LLM_BASE_URL = os.getenv("ANSWER_LLM_BASE_URL") or LLM_BASE_URL

REWRITE_LLM_PROVIDER = (os.getenv("REWRITE_LLM_PROVIDER") or ANSWER_LLM_PROVIDER).lower()
REWRITE_LLM_MODEL = os.getenv("REWRITE_LLM_MODEL") or ANSWER_LLM_MODEL
REWRITE_LLM_API_KEY = os.getenv("REWRITE_LLM_API_KEY") or ANSWER_LLM_API_KEY
REWRITE_LLM_BASE_URL = os.getenv("REWRITE_LLM_BASE_URL") or ANSWER_LLM_BASE_URL

ROUTER_LLM_PROVIDER = (os.getenv("ROUTER_LLM_PROVIDER") or ANSWER_LLM_PROVIDER).lower()
ROUTER_LLM_MODEL = os.getenv("ROUTER_LLM_MODEL") or ANSWER_LLM_MODEL
ROUTER_LLM_API_KEY = os.getenv("ROUTER_LLM_API_KEY") or ANSWER_LLM_API_KEY
ROUTER_LLM_BASE_URL = os.getenv("ROUTER_LLM_BASE_URL") or ANSWER_LLM_BASE_URL

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen3:4b")

EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "ollama").lower()
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text-v2-moe")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY") or LLM_API_KEY
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL") or LLM_BASE_URL

RERANKER_PROVIDER = os.getenv("RERANKER_PROVIDER", "bge").lower()
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-base")
RERANKER_API_KEY = os.getenv("RERANKER_API_KEY", "")
RERANKER_BASE_URL = os.getenv("RERANKER_BASE_URL", "")
RERANK_TOP_K = int(os.getenv("RERANK_TOP_K", "5"))

VECTOR_STORE_PROVIDER = os.getenv("VECTOR_STORE_PROVIDER", "chroma_embedded").lower()
METADATA_STORE_PROVIDER = os.getenv("METADATA_STORE_PROVIDER", "sqlite").lower()
OBJECT_STORE_PROVIDER = os.getenv("OBJECT_STORE_PROVIDER", "local").lower()
TASK_QUEUE_PROVIDER = os.getenv("TASK_QUEUE_PROVIDER", "inline").lower()
CACHE_PROVIDER = os.getenv("CACHE_PROVIDER", "memory").lower()

CHROMA_HTTP_HOST = os.getenv("CHROMA_HTTP_HOST", "localhost")
CHROMA_HTTP_PORT = int(os.getenv("CHROMA_HTTP_PORT", "8000"))
CHROMA_HTTP_SSL = os.getenv("CHROMA_HTTP_SSL", "false").lower() == "true"
CHROMA_HTTP_TENANT = os.getenv("CHROMA_HTTP_TENANT", "default_tenant")
CHROMA_HTTP_DATABASE = os.getenv("CHROMA_HTTP_DATABASE", "default_database")
CHROMA_HTTP_AUTH_TOKEN = os.getenv("CHROMA_HTTP_AUTH_TOKEN", "")

S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_REGION = os.getenv("S3_REGION", "us-east-1")
S3_PREFIX = os.getenv("S3_PREFIX", "").strip("/")
S3_ADDRESSING_STYLE = os.getenv("S3_ADDRESSING_STYLE", "path").lower()

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
CELERY_INDEX_QUEUE = os.getenv("CELERY_INDEX_QUEUE", "rag.index")
CELERY_INDEX_TASK_NAME = os.getenv(
    "CELERY_INDEX_TASK_NAME",
    "ragflow.index.process_job",
)
CELERY_RECONCILE_TASK_NAME = os.getenv(
    "CELERY_RECONCILE_TASK_NAME",
    "ragflow.index.reconcile",
)
CELERY_CONNECTOR_SCHEDULE_TASK_NAME = os.getenv(
    "CELERY_CONNECTOR_SCHEDULE_TASK_NAME",
    "ragflow.connectors.schedule_due",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/2")
REDIS_CACHE_PREFIX = os.getenv("REDIS_CACHE_PREFIX", "ragflow")
_local_object_store_dir = Path(
    os.getenv("LOCAL_OBJECT_STORE_DIR", str(DOC_DIR))
)
LOCAL_OBJECT_STORE_DIR = (
    _local_object_store_dir
    if _local_object_store_dir.is_absolute()
    else BASE_DIR / _local_object_store_dir
)
LOCAL_SOURCE_ALLOWED_ROOTS = [
    (Path(item.strip()) if Path(item.strip()).is_absolute() else BASE_DIR / item.strip()).resolve()
    for item in os.getenv("LOCAL_SOURCE_ALLOWED_ROOTS", str(DOC_DIR)).split(os.pathsep)
    if item.strip()
]

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
MAX_BATCH_UPLOAD_FILES = int(os.getenv("MAX_BATCH_UPLOAD_FILES", "100"))
MAX_BATCH_UPLOAD_TOTAL_MB = int(os.getenv("MAX_BATCH_UPLOAD_TOTAL_MB", "500"))
_index_worker_default = "true" if TASK_QUEUE_PROVIDER == "inline" else "false"
INDEX_WORKER_ENABLED = (
    os.getenv("INDEX_WORKER_ENABLED", _index_worker_default).lower() == "true"
)
INDEX_MAX_CONCURRENCY = int(os.getenv("INDEX_MAX_CONCURRENCY", "1"))
INDEX_JOB_LEASE_SECONDS = int(os.getenv("INDEX_JOB_LEASE_SECONDS", "300"))
INDEX_JOB_HEARTBEAT_SECONDS = int(os.getenv("INDEX_JOB_HEARTBEAT_SECONDS", "30"))
INDEX_JOB_MAX_ATTEMPTS = int(os.getenv("INDEX_JOB_MAX_ATTEMPTS", "3"))
INDEX_JOB_RECONCILE_SECONDS = int(os.getenv("INDEX_JOB_RECONCILE_SECONDS", "30"))

# --- Retrieval fusion ---
RRF_K = int(os.getenv("RRF_K", "60"))
RETRIEVAL_CANDIDATE_K = int(os.getenv("RETRIEVAL_CANDIDATE_K", "12"))
RETRIEVAL_ROUTER_MODE = os.getenv("RETRIEVAL_ROUTER_MODE", "rules").lower()
ROUTER_LLM_ENABLED = os.getenv("ROUTER_LLM_ENABLED", "false").lower() == "true"
RETRIEVAL_SCOPE_MAX_IDS = int(os.getenv("RETRIEVAL_SCOPE_MAX_IDS", "100"))
QUERY_REWRITE_CONFIDENCE_THRESHOLD = float(
    os.getenv("QUERY_REWRITE_CONFIDENCE_THRESHOLD", "0.75")
)

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
GRAPH_EXTRACTION_API_KEY = os.getenv("GRAPH_EXTRACTION_API_KEY") or LLM_API_KEY
GRAPH_EXTRACTION_BASE_URL = os.getenv("GRAPH_EXTRACTION_BASE_URL") or LLM_BASE_URL
GRAPH_AUTO_BUILD_ENABLED = _env_bool("GRAPH_AUTO_BUILD_ENABLED", True)
GRAPH_SCHEMA_VERSION = int(os.getenv("GRAPH_SCHEMA_VERSION", "1"))
GRAPH_EXTRACTION_BATCH_SIZE = int(os.getenv("GRAPH_EXTRACTION_BATCH_SIZE", "6"))
GRAPH_EXTRACTION_MAX_CHARS = int(os.getenv("GRAPH_EXTRACTION_MAX_CHARS", "6000"))
GRAPH_EXTRACTION_MAX_RETRIES = int(os.getenv("GRAPH_EXTRACTION_MAX_RETRIES", "1"))
GRAPH_MIN_CONFIDENCE = float(os.getenv("GRAPH_MIN_CONFIDENCE", "0.60"))
GRAPH_ENTITY_SEARCH_K = int(os.getenv("GRAPH_ENTITY_SEARCH_K", "5"))
GRAPH_RELATION_SEARCH_K = int(os.getenv("GRAPH_RELATION_SEARCH_K", "8"))
GRAPH_MAX_HOPS = int(os.getenv("GRAPH_MAX_HOPS", "2"))
GRAPH_MAX_RELATIONS = int(os.getenv("GRAPH_MAX_RELATIONS", "50"))
GRAPH_CACHE_ENABLED = _env_bool("GRAPH_CACHE_ENABLED", True)
GRAPH_PARALLEL_WORKERS = int(os.getenv("GRAPH_PARALLEL_WORKERS", "2"))
GRAPH_RELATED_TO_MAX_RATIO = float(os.getenv("GRAPH_RELATED_TO_MAX_RATIO", "0.35"))
GRAPH_LOW_CONFIDENCE_REVIEW_THRESHOLD = float(
    os.getenv("GRAPH_LOW_CONFIDENCE_REVIEW_THRESHOLD", "0.75")
)
GRAPH_ENTITY_VECTOR_ENABLED = _env_bool("GRAPH_ENTITY_VECTOR_ENABLED", True)
GRAPH_RELATION_VECTOR_ENABLED = _env_bool("GRAPH_RELATION_VECTOR_ENABLED", True)
GRAPH_ENTITY_MERGE_SIMILARITY = float(os.getenv("GRAPH_ENTITY_MERGE_SIMILARITY", "0.92"))
GRAPH_MERGE_CANDIDATE_LIMIT = int(os.getenv("GRAPH_MERGE_CANDIDATE_LIMIT", "200"))

# --- Graph incremental build ---
# Automatic builds debounce small uploads into one merged graph_rebuild job.
GRAPH_AUTO_BUILD_DEBOUNCE_SECONDS = int(
    os.getenv("GRAPH_AUTO_BUILD_DEBOUNCE_SECONDS", "60")
)
GRAPH_AUTO_BUILD_MIN_PENDING_CHUNKS = int(
    os.getenv("GRAPH_AUTO_BUILD_MIN_PENDING_CHUNKS", "20")
)
GRAPH_AUTO_BUILD_MAX_WAIT_SECONDS = int(
    os.getenv("GRAPH_AUTO_BUILD_MAX_WAIT_SECONDS", "300")
)
GRAPH_AUTO_BUILD_PROFILE = os.getenv("GRAPH_AUTO_BUILD_PROFILE", "fast").lower()
# Rule-based pre-filter skips chunks that clearly carry no graph facts.
GRAPH_PREFILTER_ENABLED = _env_bool("GRAPH_PREFILTER_ENABLED", True)
GRAPH_PREFILTER_MIN_CHARS = int(os.getenv("GRAPH_PREFILTER_MIN_CHARS", "24"))
# Second-stage escalation model for empty or low-confidence extractions.
GRAPH_ESCALATION_PROVIDER = os.getenv("GRAPH_ESCALATION_PROVIDER", "current").lower()
GRAPH_ESCALATION_MODEL = os.getenv("GRAPH_ESCALATION_MODEL", "")
GRAPH_ESCALATION_API_KEY = os.getenv("GRAPH_ESCALATION_API_KEY", "")
GRAPH_ESCALATION_BASE_URL = os.getenv("GRAPH_ESCALATION_BASE_URL", "")
GRAPH_ESCALATION_ENABLED = bool(
    GRAPH_ESCALATION_MODEL or GRAPH_ESCALATION_PROVIDER != "current"
)
GRAPH_ESCALATION_MIN_CONFIDENCE = float(
    os.getenv("GRAPH_ESCALATION_MIN_CONFIDENCE", "0.75")
)
# Deferred community maintenance runs in the background for stale graphs.
GRAPH_COMMUNITY_MAINTENANCE_ENABLED = _env_bool(
    "GRAPH_COMMUNITY_MAINTENANCE_ENABLED", True
)
GRAPH_COMMUNITY_MAINTENANCE_INTERVAL_SECONDS = int(
    os.getenv("GRAPH_COMMUNITY_MAINTENANCE_INTERVAL_SECONDS", "600")
)
GRAPH_COMMUNITY_MAINTENANCE_WINDOW = os.getenv(
    "GRAPH_COMMUNITY_MAINTENANCE_WINDOW", ""
).strip()
CELERY_GRAPH_MAINTENANCE_TASK_NAME = os.getenv(
    "CELERY_GRAPH_MAINTENANCE_TASK_NAME",
    "ragflow.graph.schedule_maintenance",
)

# --- Code graph ---
CODE_GRAPH_ENABLED = _env_bool("CODE_GRAPH_ENABLED", True)
CODE_PARSE_WORKERS = int(os.getenv("CODE_PARSE_WORKERS", "4"))
CODE_MAX_FILE_SIZE_MB = int(os.getenv("CODE_MAX_FILE_SIZE_MB", "2"))
CODE_SNAPSHOT_RETENTION = int(os.getenv("CODE_SNAPSHOT_RETENTION", "3"))
CODE_SEARCH_K = int(os.getenv("CODE_SEARCH_K", "12"))
CODE_VECTOR_ENABLED = _env_bool("CODE_VECTOR_ENABLED", True)
CODE_VECTOR_SEARCH_K = int(os.getenv("CODE_VECTOR_SEARCH_K", "12"))
CODE_VECTOR_MAX_CHARS = int(os.getenv("CODE_VECTOR_MAX_CHARS", "6000"))
CODE_FUNCTION_VECTOR_ENABLED = _env_bool("CODE_FUNCTION_VECTOR_ENABLED", True)
CODE_FUNCTION_SUMMARY_LLM_ENABLED = _env_bool(
    "CODE_FUNCTION_SUMMARY_LLM_ENABLED", True
)
CODE_FUNCTION_SUMMARY_BATCH_SIZE = int(
    os.getenv("CODE_FUNCTION_SUMMARY_BATCH_SIZE", "8")
)
CODE_FUNCTION_SUMMARY_MAX_SOURCE_CHARS = int(
    os.getenv("CODE_FUNCTION_SUMMARY_MAX_SOURCE_CHARS", "3000")
)
CODE_FUNCTION_SUMMARY_MAX_CHARS = int(
    os.getenv("CODE_FUNCTION_SUMMARY_MAX_CHARS", "800")
)
CODE_FUNCTION_SUMMARY_RELATION_LIMIT = int(
    os.getenv("CODE_FUNCTION_SUMMARY_RELATION_LIMIT", "12")
)

# --- Connector scheduling ---
CONNECTOR_SCHEDULER_ENABLED = _env_bool("CONNECTOR_SCHEDULER_ENABLED", True)
CONNECTOR_SCHEDULER_INTERVAL_SECONDS = int(
    os.getenv("CONNECTOR_SCHEDULER_INTERVAL_SECONDS", "60")
)
CONNECTOR_GIT_TIMEOUT_SECONDS = int(os.getenv("CONNECTOR_GIT_TIMEOUT_SECONDS", "300"))
CONNECTOR_MAX_DOWNLOAD_MB = int(os.getenv("CONNECTOR_MAX_DOWNLOAD_MB", "512"))

# --- Multi-channel retrieval ---
VECTOR_RETRIEVAL_WEIGHT = float(os.getenv("VECTOR_RETRIEVAL_WEIGHT", "0.50"))
BM25_RETRIEVAL_WEIGHT = float(os.getenv("BM25_RETRIEVAL_WEIGHT", "0.25"))
GRAPH_RETRIEVAL_WEIGHT = float(os.getenv("GRAPH_RETRIEVAL_WEIGHT", "0.25"))
CODE_RETRIEVAL_WEIGHT = float(os.getenv("CODE_RETRIEVAL_WEIGHT", "0.25"))

# Validate retrieval weights
_total_w = (
    VECTOR_RETRIEVAL_WEIGHT
    + BM25_RETRIEVAL_WEIGHT
    + GRAPH_RETRIEVAL_WEIGHT
    + CODE_RETRIEVAL_WEIGHT
)
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
if CODE_PARSE_WORKERS < 1 or GRAPH_PARALLEL_WORKERS < 1:
    raise ValueError("Parser and graph worker counts must be at least 1")
if GRAPH_ENTITY_SEARCH_K < 1 or GRAPH_RELATION_SEARCH_K < 1 or CODE_VECTOR_SEARCH_K < 1:
    raise ValueError("Graph and code search limits must be at least 1")
if CODE_VECTOR_MAX_CHARS < 100:
    raise ValueError("CODE_VECTOR_MAX_CHARS must be at least 100")
if CONNECTOR_SCHEDULER_INTERVAL_SECONDS < 10:
    raise ValueError("CONNECTOR_SCHEDULER_INTERVAL_SECONDS must be at least 10")
if not 0.0 <= GRAPH_ENTITY_MERGE_SIMILARITY <= 1.0:
    raise ValueError("GRAPH_ENTITY_MERGE_SIMILARITY must be between 0 and 1")
if GRAPH_AUTO_BUILD_DEBOUNCE_SECONDS < 0:
    raise ValueError("GRAPH_AUTO_BUILD_DEBOUNCE_SECONDS cannot be negative")
if GRAPH_AUTO_BUILD_MIN_PENDING_CHUNKS < 1:
    raise ValueError("GRAPH_AUTO_BUILD_MIN_PENDING_CHUNKS must be at least 1")
if GRAPH_AUTO_BUILD_MAX_WAIT_SECONDS < GRAPH_AUTO_BUILD_DEBOUNCE_SECONDS:
    raise ValueError("GRAPH_AUTO_BUILD_MAX_WAIT_SECONDS must be >= debounce seconds")
if GRAPH_AUTO_BUILD_PROFILE not in {"fast", "standard", "deep"}:
    raise ValueError("GRAPH_AUTO_BUILD_PROFILE must be fast, standard, or deep")
if GRAPH_PREFILTER_MIN_CHARS < 0:
    raise ValueError("GRAPH_PREFILTER_MIN_CHARS cannot be negative")
if not 0.0 <= GRAPH_ESCALATION_MIN_CONFIDENCE <= 1.0:
    raise ValueError("GRAPH_ESCALATION_MIN_CONFIDENCE must be between 0 and 1")
if GRAPH_COMMUNITY_MAINTENANCE_INTERVAL_SECONDS < 60:
    raise ValueError("GRAPH_COMMUNITY_MAINTENANCE_INTERVAL_SECONDS must be at least 60")
if GRAPH_COMMUNITY_MAINTENANCE_WINDOW and not re.match(
    r"^\d{1,2}-\d{1,2}$", GRAPH_COMMUNITY_MAINTENANCE_WINDOW
):
    raise ValueError("GRAPH_COMMUNITY_MAINTENANCE_WINDOW must look like '22-6'")

# --- Config validation ---
if CHUNK_OVERLAP >= CHUNK_SIZE:
    raise ValueError(f"CHUNK_OVERLAP ({CHUNK_OVERLAP}) must be less than CHUNK_SIZE ({CHUNK_SIZE})")
if not (0.0 <= HYBRID_VECTOR_WEIGHT <= 1.0):
    raise ValueError(f"HYBRID_VECTOR_WEIGHT must be between 0 and 1, got {HYBRID_VECTOR_WEIGHT}")
if CHUNK_SIZE < 50:
    raise ValueError(f"CHUNK_SIZE must be >= 50, got {CHUNK_SIZE}")
if RRF_K < 1:
    raise ValueError(f"RRF_K must be >= 1, got {RRF_K}")
if RETRIEVAL_ROUTER_MODE not in {"rules", "hybrid"}:
    raise ValueError("RETRIEVAL_ROUTER_MODE must be rules or hybrid")
if RETRIEVAL_SCOPE_MAX_IDS < 1:
    raise ValueError("RETRIEVAL_SCOPE_MAX_IDS must be at least 1")
if not 0.0 <= QUERY_REWRITE_CONFIDENCE_THRESHOLD <= 1.0:
    raise ValueError("QUERY_REWRITE_CONFIDENCE_THRESHOLD must be between 0 and 1")
if not (0.0 < CONTEXT_TOKEN_SAFETY_RATIO <= 1.0):
    raise ValueError(f"CONTEXT_TOKEN_SAFETY_RATIO must be in (0, 1], got {CONTEXT_TOKEN_SAFETY_RATIO}")
if RAG_MAX_CONCURRENCY < 1:
    raise ValueError("RAG_MAX_CONCURRENCY must be at least 1")
if RAG_QUEUE_TIMEOUT_SECONDS < 0:
    raise ValueError("RAG_QUEUE_TIMEOUT_SECONDS cannot be negative")
if PROVIDER_TIMEOUT_SECONDS <= 0:
    raise ValueError("PROVIDER_TIMEOUT_SECONDS must be greater than 0")
if INDEX_JOB_LEASE_SECONDS < 10:
    raise ValueError("INDEX_JOB_LEASE_SECONDS must be at least 10")
if INDEX_JOB_HEARTBEAT_SECONDS < 1:
    raise ValueError("INDEX_JOB_HEARTBEAT_SECONDS must be at least 1")
if INDEX_JOB_HEARTBEAT_SECONDS >= INDEX_JOB_LEASE_SECONDS:
    raise ValueError("INDEX_JOB_HEARTBEAT_SECONDS must be less than the lease")
if INDEX_JOB_MAX_ATTEMPTS < 1:
    raise ValueError("INDEX_JOB_MAX_ATTEMPTS must be at least 1")
if INDEX_JOB_RECONCILE_SECONDS < 5:
    raise ValueError("INDEX_JOB_RECONCILE_SECONDS must be at least 5")

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
