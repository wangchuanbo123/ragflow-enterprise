"""文档图片提取、代码感知切分、文档代码图谱三条链路的测试。"""

from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document


# ---------------------------------------------------------------------------
# 1. 图片提取链路
# ---------------------------------------------------------------------------

PNG_BYTES = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


class _MemSink:
    """内存版 ImageSink。"""

    def __init__(self):
        self.stored: dict[str, bytes] = {}

    def store(self, *, key: str, data: bytes, mime_type: str) -> str:
        self.stored[key] = data
        return f"/api/v1/objects/{key}"


def _markdown_with_image(tmp_path: Path) -> Path:
    md = tmp_path / "guide.md"
    md.write_text(
        "# 指南\n\n第一段正文。\n\n![架构图](architecture.png)\n\n结尾段落。\n",
        encoding="utf-8",
    )
    (tmp_path / "architecture.png").write_bytes(PNG_BYTES)
    return md


def test_markdown_local_image_extracted_and_stored(tmp_path):
    from rag.loaders.image_extractor import (
        attach_document_images,
        extract_document_images,
    )

    md = _markdown_with_image(tmp_path)
    images = extract_document_images(md, limit=5, max_bytes=1024 * 1024)

    assert len(images) == 1
    assert images[0].data == PNG_BYTES
    assert images[0].mime_type == "image/png"
    assert images[0].caption == "架构图"

    sink = _MemSink()
    docs = [
        Document(page_content=md.read_text(encoding="utf-8"), metadata={})
    ]
    stored = attach_document_images(docs, images, sink, object_prefix="kb1/doc1")

    assert stored and "kb1/doc1/images/img-1.png" in sink.stored
    assert "[[img-1]]" in docs[-1].page_content
    assert docs[-1].metadata["images"][0]["url"].startswith("/api/v1/objects/")


def test_docx_media_images_extracted(tmp_path):
    import zipfile

    docx = tmp_path / "spec.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types/>',
        )
        archive.writestr("word/document.xml", "<w:document/>")
        archive.writestr("word/media/image1.png", PNG_BYTES)

    from rag.loaders.image_extractor import extract_document_images

    images = extract_document_images(docx, limit=5, max_bytes=1024 * 1024)
    assert len(images) == 1
    assert images[0].data == PNG_BYTES
    assert images[0].mime_type == "image/png"


def test_remote_markdown_image_kept_as_url_without_download(tmp_path):
    from rag.loaders.image_extractor import attach_document_images, extract_document_images

    md = tmp_path / "remote.md"
    md.write_text(
        "![远程图](https://example.com/pic.png)\n",
        encoding="utf-8",
    )
    images = extract_document_images(md, limit=5, max_bytes=1024 * 1024)
    assert len(images) == 1
    assert images[0].data is None
    assert images[0].url == "https://example.com/pic.png"

    sink = _MemSink()
    docs = [Document(page_content=md.read_text(encoding="utf-8"), metadata={})]
    stored = attach_document_images(docs, images, sink, object_prefix="kb/r")
    # 远程图沿用原 URL，不写对象存储
    assert sink.stored == {}
    assert stored and stored[0].url == "https://example.com/pic.png"


def test_images_scoped_to_own_chunk_and_rendered_in_context(tmp_path):
    from rag.context.context_builder import build_context
    from rag.indexing.chunker import split_documents
    from rag.loaders.image_extractor import (
        attach_document_images,
        extract_document_images,
    )

    md = _markdown_with_image(tmp_path)
    images = extract_document_images(md, limit=5, max_bytes=1024 * 1024)
    sink = _MemSink()
    docs = [Document(page_content=md.read_text(encoding="utf-8"), metadata={})]
    attach_document_images(docs, images, sink, object_prefix="kb1/doc1")

    result = split_documents(
        docs, strategy="code_aware", document_id="doc1", file_hash_val="h1"
    )
    with_images = [c for c in result.chunks if c.metadata.get("images")]
    without_images = [c for c in result.chunks if not c.metadata.get("images")]

    assert with_images, "占位符所在 chunk 应保留图片"
    assert all(
        item["marker"] == "img-1"
        for c in with_images
        for item in c.metadata["images"]
    )

    context, sources = build_context(with_images)
    assert "![架构图](/api/v1/objects/" in context
    assert sources and sources[0]["images"][0]["marker"] == "img-1"

    # 无图片 chunk 的上下文不应出现图片引用
    if without_images:
        plain_context, _ = build_context(without_images)
        assert "/api/v1/objects/" not in plain_context


def test_image_metadata_json_roundtrip():
    from rag.indexing.image_metadata import decode_images, encode_images_for_vector

    images = [
        {"marker": "img-1", "url": "/api/v1/objects/kb/img-1.png", "caption": "图"},
    ]
    encoded = encode_images_for_vector(images)
    assert isinstance(encoded, str)
    decoded = decode_images(encoded)
    assert decoded == images

    assert decode_images(None) == []
    assert decode_images("not-json") == []
    assert decode_images(images) == images


def test_context_exposes_structured_verbatim_code_snippets():
    from rag.artifacts import collect_source_artifacts
    from rag.context.context_builder import build_context

    doc = Document(
        page_content="说明\n```python\ndef recover():\n    return True\n```",
        metadata={
            "source": "recovery.md",
            "document_id": "doc-code",
            "chunk_id": "chunk-code",
            "contains_code": True,
        },
    )

    _, sources = build_context([doc])
    images, snippets = collect_source_artifacts(sources)

    assert images == []
    assert sources[0]["contains_code"] is True
    assert sources[0]["code_snippets"][0]["content"] == "def recover():\n    return True"
    assert snippets[0]["is_verbatim"] is True
    assert snippets[0]["path"] == "recovery.md"
    assert snippets[0]["document_id"] == "doc-code"


# ---------------------------------------------------------------------------
# 2. 代码感知切分
# ---------------------------------------------------------------------------

CODE_DOC = """# 使用说明

这是第一段正文。

```python
def calculate(value):
    total = 0
    for item in value:
        total += item
    return total
```

第二段正文。

<pre><code>
SELECT id, name FROM users;
</code></pre>

结尾段落。
"""


def test_short_code_block_stays_whole():
    from rag.indexing.chunker import split_documents

    result = split_documents(
        [Document(page_content=CODE_DOC, metadata={})],
        strategy="code_aware",
        document_id="d1",
        file_hash_val="h1",
    )
    hits = [c for c in result.chunks if "def calculate" in c.page_content]
    assert hits and "return total" in hits[0].page_content
    assert hits[0].metadata.get("contains_code") is True


def test_pre_block_recognized_as_code():
    from rag.indexing.chunker import split_documents

    result = split_documents(
        [Document(page_content=CODE_DOC, metadata={})],
        strategy="code_aware",
        document_id="d1",
        file_hash_val="h1",
    )
    hits = [c for c in result.chunks if "SELECT id" in c.page_content]
    assert hits and hits[0].metadata.get("contains_code") is True


def test_plain_text_not_flagged_as_code():
    from rag.indexing.chunker import split_documents

    result = split_documents(
        [Document(page_content=CODE_DOC, metadata={})],
        strategy="code_aware",
        document_id="d1",
        file_hash_val="h1",
    )
    hits = [c for c in result.chunks if "这是第一段正文" in c.page_content]
    assert hits and not hits[0].metadata.get("contains_code")


def test_long_code_splits_at_boundary_with_fence_restored():
    from rag.indexing.chunker import split_documents

    body = "\n".join(f"    line_{i} = {i}  # {'x' * 40}" for i in range(80))
    doc = f"```python\ndef long_function():\n{body}\n```\n"
    result = split_documents(
        [Document(page_content=doc, metadata={})],
        strategy="code_aware",
        document_id="d1",
        file_hash_val="h1",
    )
    code_chunks = [c for c in result.chunks if c.metadata.get("contains_code")]
    assert len(code_chunks) >= 2, "超长代码应拆多片"
    # 每片都以围栏开头/收尾，仍是合法 markdown 代码块
    assert all(c.page_content.lstrip().startswith("```") for c in code_chunks)
    assert all(c.page_content.rstrip().endswith("```") for c in code_chunks)


def test_code_aware_chunk_id_deterministic():
    from rag.indexing.chunker import split_documents

    doc = Document(page_content=CODE_DOC, metadata={})
    first = split_documents([doc], strategy="code_aware", document_id="d", file_hash_val="h")
    second = split_documents([doc], strategy="code_aware", document_id="d", file_hash_val="h")
    assert [c.metadata["chunk_id"] for c in first.chunks] == [
        c.metadata["chunk_id"] for c in second.chunks
    ]


# ---------------------------------------------------------------------------
# 3. 文档代码图谱
# ---------------------------------------------------------------------------


def _seed_document_with_code(db_engine, tmp_path, *, content: str):
    """建 KB → 文档 → chunk，模拟已完成的文档索引。"""
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument

    with SessionLocal() as db:
        document = KnowledgeDocument(
            id="doc-code-1",
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            source_path=f"knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/doc-code-1/guide.md",
            original_filename="guide.md",
            file_hash="hash-1",
            file_size=len(content),
            status="ready",
        )
        db.add(document)
        db.add(KnowledgeChunk(
            id="chunk-code-1",
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            document_id="doc-code-1",
            file_hash="hash-1",
            chunk_index=0,
            content=content,
            content_hash="chash-1",
            token_count=42,
        ))
        db.commit()
    return DEFAULT_KNOWLEDGE_BASE_ID


DOC_WITH_CODE = """# 开发指南

示例函数：

```python
import json

def parse_config(raw):
    return json.loads(raw)

class Loader:
    def load(self, path):
        return parse_config(path)
```

普通正文段落。
"""


def test_document_code_graph_builds_symbols_from_chunks(db_engine, tmp_path):
    from app.adapters.local import LocalObjectStore
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.repositories.code_graph_repository import CodeGraphRepository
    from app.services.code_index_service import CodeIndexService

    _seed_document_with_code(db_engine, tmp_path, content=DOC_WITH_CODE)

    with SessionLocal() as db:
        service = CodeIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=LocalObjectStore(tmp_path / "objects"),
            task_queue=object(),
        )
        result = service.index_document_code()

    assert result["status"] == "completed"
    assert result["code_blocks"] >= 1
    assert result["symbols"] >= 3  # FILE + parse_config + Loader + load
    assert result["resolved_relations"] >= 1  # load CALLS parse_config

    with SessionLocal() as db:
        repository = CodeGraphRepository(db, DEFAULT_KNOWLEDGE_BASE_ID)
        symbols = repository.search_symbols("parse_config", 5)
        assert symbols, "文档内函数应可检索"
        assert any(
            item["path"].startswith("docs/doc-code-1/") for item in symbols
        ), "虚拟路径应以 docs/{document_id}/ 开头"


def test_document_code_graph_incremental_and_unchanged(db_engine, tmp_path):
    from app.adapters.local import LocalObjectStore
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.code_index_service import CodeIndexService

    _seed_document_with_code(db_engine, tmp_path, content=DOC_WITH_CODE)

    with SessionLocal() as db:
        service = CodeIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=LocalObjectStore(tmp_path / "objects"),
            task_queue=object(),
        )
        first = service.index_document_code()
        second = service.index_document_code()

    assert first["status"] == "completed"
    assert second["status"] == "unchanged"


def test_document_code_graph_disabled_returns_disabled(db_engine, tmp_path, monkeypatch):
    import app.core.config as config_module
    from app.adapters.local import LocalObjectStore
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.code_index_service import CodeIndexService

    monkeypatch.setattr(config_module, "DOCUMENT_CODE_GRAPH_ENABLED", False)
    _seed_document_with_code(db_engine, tmp_path, content=DOC_WITH_CODE)

    with SessionLocal() as db:
        service = CodeIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=LocalObjectStore(tmp_path / "objects"),
            task_queue=object(),
        )
        result = service.index_document_code()

    assert result["status"] == "disabled"


def test_directory_index_merges_document_code(db_engine, tmp_path):
    from app.adapters.local import LocalObjectStore
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.repositories.code_graph_repository import CodeGraphRepository
    from app.services.code_index_service import CodeIndexService

    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text(
        "def target():\n    return 1\n",
        encoding="utf-8",
    )
    _seed_document_with_code(db_engine, tmp_path, content=DOC_WITH_CODE)

    with SessionLocal() as db:
        service = CodeIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=LocalObjectStore(tmp_path / "objects"),
            task_queue=object(),
        )
        result = service.index_directory(
            source,
            data_source_id="ds-1",
            merge_document_code=True,
        )

    assert result["status"] == "completed"
    # 目录文件 + 文档代码块都在一个快照里
    assert result["files"] >= 2
    with SessionLocal() as db:
        repository = CodeGraphRepository(db, DEFAULT_KNOWLEDGE_BASE_ID)
        assert repository.search_symbols("target", 5), "目录符号应存在"
        assert repository.search_symbols("parse_config", 5), "文档代码符号应合并进来"


def test_document_code_retriever_returns_document_source(db_engine, tmp_path):
    """代码检索命中文档代码时，应带 document_source 便于跳回原文。"""
    from app.adapters.local import LocalObjectStore
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services.code_index_service import CodeIndexService
    from rag.code_graph.retriever import CodeGraphRetriever

    _seed_document_with_code(db_engine, tmp_path, content=DOC_WITH_CODE)

    with SessionLocal() as db:
        service = CodeIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=LocalObjectStore(tmp_path / "objects"),
            task_queue=object(),
        )
        service.index_document_code()

    retriever = CodeGraphRetriever(DEFAULT_KNOWLEDGE_BASE_ID)
    docs = retriever.get_relevant_documents("parse_config")
    assert docs
    assert any(
        (d.metadata.get("qualified_name") or "").endswith("parse_config")
        for d in docs
    )
