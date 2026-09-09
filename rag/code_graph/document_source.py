"""把已入库文档中的代码块提升为代码图谱可检索的符号。

与 Git/本地目录代码源不同，文档内代码散落在 chunk 里、没有真实的文件系统。
这里把它抽象成一个"虚拟代码源"：

- 每个含代码块的 chunk 视为一个虚拟文件，路径形如 ``docs/{document_id}/chunk-{n}-block-{ordinal}.py``；
- 每个代码块用 CodeParser 解析成符号与关系（含 FILE 符号）；
- 产出与 ``index_directory`` 相同的 manifest 结构，因此快照构建、增量复用、
  关系解析、向量重建可以完全复用现有链路。
"""

from __future__ import annotations

import hashlib

from rag.code_graph.parser import LANGUAGE_BY_SUFFIX, CodeParser
from rag.code_graph.schemas import ParsedFile
from rag.indexing.code_segments import iter_code_segments

_DOCUMENT_PARSER_VERSION = "doc-code-v1"

# 语言名 → 能被 LANGUAGE_BY_SUFFIX 识别的标准文件后缀。
_LANGUAGE_SUFFIX = {}
for _suffix, _language in LANGUAGE_BY_SUFFIX.items():
    _LANGUAGE_SUFFIX.setdefault(_language, _suffix)


def document_code_parser_version() -> str:
    """文档代码提取自身的版本；与 PARSER_VERSION 组合参与快照复用判断。"""
    return _DOCUMENT_PARSER_VERSION


def _virtual_path(document_id: str, chunk_index: int, language: str | None, ordinal: int) -> str:
    suffix = _LANGUAGE_SUFFIX.get(language or "", ".txt")
    return f"docs/{document_id}/chunk-{chunk_index}-block-{ordinal}{suffix}"


def extract_document_code(
    chunk_contents: list[tuple[str, int, str]],
    parser: CodeParser | None = None,
    *,
    max_blocks: int = 60,
    min_block_lines: int = 3,
) -> list[ParsedFile]:
    """把文档 chunk 里的代码块解析成 ParsedFile 列表。

    ``chunk_contents`` 是 ``(document_id, chunk_index, content)`` 三元组列表。
    返回的每个 ParsedFile 对应一个虚拟文件（一个代码块）。
    """
    parser = parser or CodeParser()
    parsed_files: list[ParsedFile] = []
    processed_blocks = 0

    for document_id, chunk_index, content in chunk_contents:
        if not content or processed_blocks >= max_blocks:
            break
        for ordinal, segment in enumerate(
            iter_code_segments(
                content,
                min_block_lines=min_block_lines,
                include_indented=False,
            ),
            start=1,
        ):
            if processed_blocks >= max_blocks:
                break
            if not segment.text.strip():
                continue
            text = segment.text + "\n"
            virtual = _virtual_path(
                document_id, chunk_index, segment.language, ordinal
            )
            parsed = parser.parse(virtual, text)
            parsed.path = virtual
            parsed.parser_backend = (
                f"document:{parsed.parser_backend}"
            )
            # 记录来源，检索时能跳回原文档。
            for symbol in parsed.symbols:
                symbol.metadata.setdefault(
                    "document_source",
                    {
                        "document_id": document_id,
                        "chunk_index": chunk_index,
                        "language": segment.language,
                    },
                )
            parsed_files.append(parsed)
            processed_blocks += 1

    return parsed_files


def manifest_for_document_chunks(
    parsed_files: list[ParsedFile],
) -> tuple[list[tuple[str, str]], dict[str, bytes]]:
    """把 ParsedFile 列表转成 manifest 与源码字节映射（与目录索引同构）。"""
    manifest: list[tuple[str, str]] = []
    source_by_path: dict[str, bytes] = {}
    for parsed in parsed_files:
        source_text = _reconstruct_source(parsed)
        data = source_text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        manifest.append((parsed.path, digest))
        source_by_path[parsed.path] = data
    return manifest, source_by_path


def _reconstruct_source(parsed: ParsedFile) -> str:
    """从符号摘录重建虚拟文件内容，保证 excerpt 与文件内容一致。"""
    lines: list[str] = []
    max_line = 0
    for symbol in parsed.symbols:
        max_line = max(max_line, symbol.end_line)
    if max_line == 0:
        max_line = 1
    # ParsedSymbol.source_excerpt 已包含真实代码；按 start_line 对齐。
    filled: dict[int, str] = {}
    for symbol in parsed.symbols:
        excerpt = (symbol.source_excerpt or "").split("\n")
        for offset, line in enumerate(excerpt):
            filled.setdefault(symbol.start_line + offset, line)
    for number in range(1, max_line + 1):
        lines.append(filled.get(number, ""))
    return "\n".join(lines) + "\n"
