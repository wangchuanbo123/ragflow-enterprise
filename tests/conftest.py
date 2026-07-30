"""共享测试夹具：隔离的内存 SQLite 数据库与 Fake RAG 运行时。"""

from pathlib import Path
from sys import path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in path:
    path.insert(0, str(ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def db_engine(monkeypatch):
    """创建内存 SQLite 引擎并建表，替换全局 SessionLocal。"""
    import app.core.database as db_module
    from app.models import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", SessionLocal)
    return engine


@pytest.fixture
def client(db_engine, monkeypatch):
    """带初始化管理员的测试客户端。"""
    from app.core import security
    from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME

    monkeypatch.setattr(
        security,
        "JWT_SECRET_KEY",
        "test-secret-key-for-unit-tests-32-bytes",
    )

    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User

    with SessionLocal() as db:
        if db.query(User).count() == 0:
            db.add(
                User(
                    username=INITIAL_ADMIN_USERNAME,
                    password_hash=hash_password(INITIAL_ADMIN_PASSWORD),
                    display_name=INITIAL_ADMIN_USERNAME,
                    role="admin",
                    is_active=True,
                )
            )
            db.commit()

    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    yield c


class FakeStreamLLM:
    """测试用 LLM Provider：支持 generate 与真实流式 stream。"""

    def __init__(self, answer: str = "这是测试答案。", chunks=None):
        self.answer = answer
        self.chunks = chunks or ["这", "是", "测试", "答案。"]
        self.calls: list[str] = []

    def get_model(self):
        return self

    def generate(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.answer

    def stream(self, prompt: str):
        self.calls.append(prompt)
        for c in self.chunks:
            yield c


class FakeRetriever:
    """测试用 Retriever：兼容 RRF 接口。"""

    def __init__(self):
        self.queries: list[str] = []
        self._docs = None

    def get_relevant_documents(self, query: str):
        from langchain_core.documents import Document

        self.queries.append(query)
        if self._docs is not None:
            return self._docs
        return [
            Document(page_content="主要资料内容", metadata={
                "source": "primary.txt",
                "chunk_id": "c1",
                "content_hash": "h1",
            }),
        ]

    def retrieve_single(self, query: str):
        return self.get_relevant_documents(query)

    def retrieve_multi_query(self, queries: list):
        results = []
        for q in queries:
            results.extend(self.get_relevant_documents(q))
        # Deduplicate by chunk_id
        seen = set()
        deduped = []
        for doc in results:
            cid = doc.metadata.get("chunk_id")
            if cid not in seen:
                seen.add(cid)
                deduped.append(doc)
        return deduped


class FakeReranker:
    def rerank(self, query, docs, top_k=None):
        del query, top_k
        return list(docs)


def make_fake_runtime(answer: str = "这是测试答案。", chunks=None):
    from rag.runtime.runtime import RAGRuntime

    return RAGRuntime(
        llm=FakeStreamLLM(answer=answer, chunks=chunks),
        retriever=FakeRetriever(),
        reranker=FakeReranker(),
    )
