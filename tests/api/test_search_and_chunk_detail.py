"""全文搜索与片段详情端点测试。"""

from pathlib import Path
from sys import path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in path:
    path.insert(0, str(ROOT))

import pytest


def _login(client):
    from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME

    res = client.post(
        "/api/v1/auth/login",
        json={"username": INITIAL_ADMIN_USERNAME, "password": INITIAL_ADMIN_PASSWORD},
    )
    assert res.status_code == 200, res.text
    return res.cookies


@pytest.fixture
def auth_client(client):
    _login(client)
    return client


KB_ID = "00000000-0000-0000-0000-000000000001"


def _seed_chunks(client):
    """直接向数据库写入一份文档与三个相邻 chunk。"""
    from app.core.database import SessionLocal
    from app.models.knowledge_document import KnowledgeDocument
    from app.models.knowledge_chunk import KnowledgeChunk

    with SessionLocal() as db:
        db.merge(KnowledgeDocument(
            id="doc-1",
            knowledge_base_id=KB_ID,
            source_path=f"{KB_ID}/documents/doc-1.pdf",
            original_filename="测试手册.pdf",
            file_hash="hash-1",
            file_size=1024,
            status="ready",
        ))
        for index in (1, 2, 3):
            db.merge(KnowledgeChunk(
                id=f"chunk-{index}",
                knowledge_base_id=KB_ID,
                document_id="doc-1",
                file_hash="hash-1",
                chunk_index=index,
                content=f"第 {index} 段内容：包含独特关键词 alpha{index}。",
                content_hash=f"hash-chunk-{index}",
                token_count=20,
                page=index,
                section="第一节",
                metadata_json={
                    "images": [
                        {
                            "marker": "img-1",
                            "url": f"{KB_ID}/objects/img-{index}.png",
                            "caption": "示意图",
                            "mime_type": "image/png",
                            "page": index,
                            "width": 800,
                            "height": 600,
                            "filename": f"img-{index}.png",
                        }
                    ]
                },
                graph_status="pending",
                graph_schema_version=1,
            ))
        db.commit()


def test_search_requires_query(auth_client):
    res = auth_client.get(f"/api/v1/knowledge-bases/{KB_ID}/search")
    assert res.status_code == 422


def test_search_returns_hits(auth_client, monkeypatch):
    _seed_chunks(auth_client)

    class FakeBM25:
        def get_relevant_documents(self, query, top_k=10):
            from langchain_core.documents import Document

            return [
                Document(
                    page_content="第 2 段内容：包含独特关键词 alpha2。",
                    metadata={
                        "chunk_id": "chunk-2",
                        "document_id": "doc-1",
                        "chunk_index": 2,
                        "page": 2,
                        "section": "第一节",
                        "bm25_score": 12.5,
                    },
                )
            ]

    import app.api.v1.knowledge_bases as kb_api

    monkeypatch.setattr(
        "rag.retrievers.bm25_retriever.get_bm25_retriever",
        lambda kb_id: FakeBM25(),
    )
    res = auth_client.get(f"/api/v1/knowledge-bases/{KB_ID}/search", params={"query": "alpha2"})
    assert res.status_code == 200, res.text
    hits = res.json()
    assert len(hits) == 1
    hit = hits[0]
    assert hit["chunk_id"] == "chunk-2"
    assert hit["document_name"] == "测试手册.pdf"
    assert hit["score"] == pytest.approx(12.5)
    assert "alpha2" in hit["snippet"]
    # BM25 检索的 doc metadata 没有 images，命中不返回图片
    assert hit["images"] == []


def test_search_snippet_surrounds_match(auth_client, monkeypatch):
    long_text = "开头文字 " + "填充" * 120 + " 独特关键词 needle " + "尾部" * 50
    _seed_chunks(auth_client)

    class FakeBM25:
        def get_relevant_documents(self, query, top_k=10):
            from langchain_core.documents import Document

            return [
                Document(
                    page_content=long_text,
                    metadata={
                        "chunk_id": "chunk-3",
                        "document_id": "doc-1",
                        "chunk_index": 3,
                        "page": 3,
                        "section": "第一节",
                        "bm25_score": 5.0,
                    },
                )
            ]

    monkeypatch.setattr(
        "rag.retrievers.bm25_retriever.get_bm25_retriever",
        lambda kb_id: FakeBM25(),
    )
    res = auth_client.get(f"/api/v1/knowledge-bases/{KB_ID}/search", params={"query": "needle"})
    assert res.status_code == 200
    snippet = res.json()[0]["snippet"]
    assert "needle" in snippet
    assert len(snippet) < len(long_text)


def test_get_chunk_detail_with_neighbors(auth_client):
    _seed_chunks(auth_client)

    res = auth_client.get(f"/api/v1/knowledge-bases/{KB_ID}/chunks/chunk-2?context=1")
    assert res.status_code == 200, res.text
    detail = res.json()
    assert detail["chunk_id"] == "chunk-2"
    assert detail["document_name"] == "测试手册.pdf"
    assert detail["chunk_index"] == 2
    assert "alpha2" in detail["content"]
    assert [n["chunk_index"] for n in detail["previous"]] == [1]
    assert [n["chunk_index"] for n in detail["next"]] == [3]
    assert len(detail["images"]) == 1
    assert detail["images"][0]["caption"] == "示意图"


def test_get_chunk_detail_context_zero(auth_client):
    _seed_chunks(auth_client)

    res = auth_client.get(f"/api/v1/knowledge-bases/{KB_ID}/chunks/chunk-1?context=0")
    assert res.status_code == 200
    detail = res.json()
    assert detail["previous"] == []
    # context=0 时 chunk_index 范围 [1,1]，只含自身
    assert detail["next"] == []


def test_get_chunk_detail_not_found(auth_client):
    res = auth_client.get(f"/api/v1/knowledge-bases/{KB_ID}/chunks/missing")
    assert res.status_code == 404


def test_get_chunk_detail_isolation(auth_client):
    """跨知识库访问应 404。"""
    _seed_chunks(auth_client)
    other_kb = "00000000-0000-0000-0000-000000000002"
    res = auth_client.get(f"/api/v1/knowledge-bases/{other_kb}/chunks/chunk-2")
    assert res.status_code == 404
