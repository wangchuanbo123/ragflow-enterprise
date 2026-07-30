"""切片器测试。"""

from langchain_core.documents import Document

from rag.indexing.chunker import (
    content_hash,
    count_tokens,
    deterministic_chunk_id,
    split_documents,
)


def test_recursive_split_produces_chunks():
    text = "这是第一段内容。" * 50 + "\n\n" + "这是第二段内容。" * 50
    doc = Document(page_content=text, metadata={"source": "test.txt"})
    result = split_documents(
        [doc],
        strategy="recursive",
        chunk_size=100,
        chunk_overlap=20,
        min_chunk_size=10,
        document_id="doc1",
        file_hash_val="hash1",
    )
    assert len(result.chunks) >= 2
    for chunk in result.chunks:
        assert chunk.metadata["chunk_id"]
        assert chunk.metadata["content_hash"]
        assert chunk.metadata["chunk_index"] is not None


def test_deterministic_chunk_id_is_stable():
    cid1 = deterministic_chunk_id("doc1", "hash1", 0, 2)
    cid2 = deterministic_chunk_id("doc1", "hash1", 0, 2)
    assert cid1 == cid2

    cid3 = deterministic_chunk_id("doc1", "hash1", 1, 2)
    assert cid1 != cid3


def test_deduplicate_identical_chunks():
    text = "完全相同的内容。"
    doc1 = Document(page_content=text, metadata={"source": "a.txt"})
    doc2 = Document(page_content=text, metadata={"source": "b.txt"})
    result = split_documents([doc1, doc2], chunk_size=200, chunk_overlap=0, min_chunk_size=0)
    assert len(result.chunks) == 1


def test_small_tail_merge():
    text = "主要内容内容内容。" * 20 + "\n短"
    doc = Document(page_content=text, metadata={"source": "test.txt"})
    result = split_documents(
        [doc],
        chunk_size=100,
        chunk_overlap=10,
        min_chunk_size=20,
    )
    # The tiny tail "短" should merge into previous chunk
    last_chunk = result.chunks[-1]
    assert count_tokens(last_chunk.page_content) > 5


def test_content_hash_deterministic():
    h1 = content_hash("hello world")
    h2 = content_hash("hello world")
    h3 = content_hash("hello  world")
    assert h1 == h2
    assert h1 != h3
