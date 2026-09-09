"""代码块感知的文档切分。

与 recursive 策略的区别：先识别文档里的代码块（markdown 围栏、``<pre>`` 标签、
缩进代码块），把代码段整体保护起来，只在代码本身超过预算时才沿函数/类边界拆分，
绝不把代码切在任意换行处。正文段仍走 RecursiveCharacterTextSplitter。
"""

from __future__ import annotations

from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_core.documents import Document

from rag.indexing.code_segments import Segment, extract_segments
from rag.indexing.tokens import count_tokens

_SEPARATORS = [
    "\n\n\n",
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    ". ",
    "! ",
    "? ",
    " ",
    "",
]


def _strip_strings_and_comments(line: str) -> str:
    out: list[str] = []
    quote: str | None = None
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if char == "#" or line[index : index + 2] == "//":
            break
        out.append(char)
        index += 1
    return "".join(out)


def _depth_after(depth: int, line: str) -> int:
    code = _strip_strings_and_comments(line)
    delta = (
        code.count("{")
        + code.count("(")
        + code.count("[")
        - code.count("}")
        - code.count(")")
        - code.count("]")
    )
    return max(0, depth + delta)


def _boundary_indices(lines: list[str], language: str | None) -> list[int]:
    """找出可以作为拆分点的行号：括号深度归零且该行顶格书写。"""
    indices = [0]
    depth = 0
    for index, line in enumerate(lines):
        if index == 0:
            depth = _depth_after(depth, line)
            continue
        stripped = line.strip()
        if stripped and not line[:1].isspace() and depth <= 0:
            indices.append(index)
        depth = _depth_after(depth, line)
    return indices


def _largest_fit(lines: list[str], start: int, limit: int, budget: int) -> int:
    lo, hi = start + 1, limit
    best = start + 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if count_tokens("\n".join(lines[start:mid])) <= budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _pack_lines(
    lines: list[str],
    budget: int,
    language: str | None,
    overlap_lines: int,
) -> list[list[str]]:
    if not lines:
        return []
    if count_tokens("\n".join(lines)) <= budget:
        return [lines]

    boundaries = _boundary_indices(lines, language)
    atoms: list[tuple[int, int]] = []
    for start, end in zip(boundaries, boundaries[1:] + [len(lines)]):
        if count_tokens("\n".join(lines[start:end])) <= budget:
            atoms.append((start, end))
            continue
        cursor = start
        while cursor < end:
            stop = _largest_fit(lines, cursor, end, budget)
            if stop <= cursor:
                stop = cursor + 1
            atoms.append((cursor, stop))
            cursor = stop

    if not atoms:
        return [lines]

    groups: list[tuple[int, int]] = []
    current_start, current_end = atoms[0]
    for start, end in atoms[1:]:
        if count_tokens("\n".join(lines[current_start:end])) <= budget:
            current_end = end
            continue
        groups.append((current_start, current_end))
        current_start = max(start, current_end - overlap_lines)
        if current_start >= current_end:
            current_start = start
        current_end = end
    groups.append((current_start, current_end))
    return [lines[start:end] for start, end in groups if end > start]


def _render_code(segment: Segment, body: str) -> str:
    """把代码段重新包回它在原文里的外衣，保证切片仍是合法富文本。"""
    if segment.fence in {"```", "~~~"}:
        language = segment.language or ""
        return f"{segment.fence}{language}\n{body}\n{segment.fence}"
    if segment.fence == "<pre>":
        return f"<pre><code>\n{body}\n</code></pre>"
    indent = segment.indent
    return "\n".join(
        f"{indent}{line}" if line.strip() else "" for line in body.split("\n")
    )


def _split_code_segment(
    segment: Segment,
    chunk_size: int,
    overlap_lines: int,
) -> list[str]:
    overhead = count_tokens(_render_code(segment, ""))
    budget = max(32, chunk_size - overhead)
    lines = segment.text.split("\n")
    return [
        _render_code(segment, "\n".join(group))
        for group in _pack_lines(lines, budget, segment.language, overlap_lines)
    ]


def split_documents_code_aware(
    docs,
    chunk_size: int,
    chunk_overlap: int,
    min_chunk_size: int,
    *,
    min_block_lines: int = 3,
) -> list[Document]:
    """按"代码段整体保留、正文段递归切分"的方式切片。"""
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=_SEPARATORS,
    )
    overlap_lines = max(1, chunk_overlap // 8)
    documents: list[Document] = []

    for doc in docs:
        segments = extract_segments(doc.page_content, min_block_lines=min_block_lines)
        pieces: list[tuple[str, bool]] = []
        for segment in segments:
            if not segment.text.strip():
                continue
            if segment.is_code:
                for piece in _split_code_segment(segment, chunk_size, overlap_lines):
                    pieces.append((piece, True))
            else:
                for piece in splitter.split_text(segment.text):
                    pieces.append((piece, False))

        merged: list[tuple[str, bool]] = []
        for text, is_code in pieces:
            if (
                not is_code
                and merged
                and not merged[-1][1]
                and min_chunk_size > 0
                and count_tokens(text) < min_chunk_size
            ):
                merged[-1] = (merged[-1][0] + "\n" + text, False)
            else:
                merged.append((text, is_code))

        for text, is_code in merged:
            metadata = dict(doc.metadata)
            if is_code:
                metadata["contains_code"] = True
            documents.append(Document(page_content=text, metadata=metadata))

    return documents
