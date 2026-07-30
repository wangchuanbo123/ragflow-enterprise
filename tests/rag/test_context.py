"""ContextBuilder 测试。"""

from langchain_core.documents import Document

from rag.context.context_builder import build_context


def _make_doc(content: str, source: str = "a.txt", chunk_id: str = "c1", content_hash: str = "h1"):
    return Document(page_content=content, metadata={
        "source": source,
        "chunk_id": chunk_id,
        "content_hash": content_hash,
        "page": 1,
        "section": "intro",
    })


def test_empty_docs_returns_no_info():
    context, sources = build_context([])
    assert "没有" in context
    assert sources == []


def test_dedup_by_content_hash():
    doc1 = _make_doc("相同内容", content_hash="same")
    doc2 = _make_doc("相同内容", chunk_id="c2", content_hash="same")
    context, sources = build_context([doc1, doc2])
    assert len(sources) == 1


def test_citation_ids_are_sequential():
    docs = [
        _make_doc("内容1", chunk_id="c1", content_hash="h1"),
        _make_doc("内容2", chunk_id="c2", content_hash="h2"),
    ]
    context, sources = build_context(docs)
    assert sources[0]["citation_id"] == "来源1"
    assert sources[1]["citation_id"] == "来源2"
    assert "[来源1]" in context
    assert "[来源2]" in context


def test_source_is_not_absolute_path():
    doc = _make_doc("content", source="data/docs/folder/file.txt")
    context, sources = build_context([doc])
    assert "data/" not in sources[0]["source"]
    assert "D:\\" not in sources[0]["source"]


def test_per_source_quota():
    docs = [
        _make_doc(f"内容{i}", source="same.txt", chunk_id=f"c{i}", content_hash=f"h{i}")
        for i in range(5)
    ]
    context, sources = build_context(docs)
    # MAX_CHUNKS_PER_SOURCE defaults to 2
    assert len(sources) <= 2
