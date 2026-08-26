"""规则预过滤：跳过明显不含图谱信息的 Chunk，节省 LLM 抽取调用。

覆盖的典型噪音：
- 纯目录/导航行（点线引导符 + 页码）
- 页眉页脚、版权声明、机密水印
- 只有编号或格式标记的碎片
- 无完整句子的表格碎片
- 重复模板行
"""

from __future__ import annotations

import re

_BULLET_PREFIX = re.compile(
    r"^[\s]*([-*•·—–]|\d+[.)、]|[a-zA-Z][.)]|[（(]\d+[)）]|[第]\s*\d+\s*[章节条])"
)
_PAGE_REFERENCE = re.compile(r"([.·…\s]{2,}\d+|\.{3,}\s*\d+|…+\s*\d+)\s*$")
_SENTENCE_END = re.compile(r"[。．！？；;!?\.]")
_TABLE_SEPARATOR = re.compile(r"[|｜]")
_BOILERPLATE = re.compile(
    r"版权所有|保留所有权利|all\s+rights\s+reserved|copyright\s+©|©\s*\d{4}"
    r"|机密文件|strictly\s+confidential|confidential"
    r"|第\s*\d+\s*页\s*[，,/共]|page\s+\d+\s+of\s+\d+|共\s*\d+\s*页",
    re.IGNORECASE,
)
_ONLY_SYMBOLS = re.compile(r"^[\W_]+$", re.UNICODE)
_ONLY_NUMBERS = re.compile(r"^[\d\s.,:;|/\\\-—–_()%#*+×=…·]+$")


def _visible(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _visible_len(text: str) -> int:
    return len(_visible(text))


def has_graph_signal(content: str, *, min_chars: int = 24) -> bool:
    """判断 Chunk 是否值得调用 LLM 抽取；False 表示可以直接标记完成。"""
    text = (content or "").strip()
    if not text:
        return False
    if _visible_len(text) < min_chars:
        return False
    if _ONLY_SYMBOLS.match(text) or _ONLY_NUMBERS.match(text):
        return False

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    content_lines = [line for line in lines if not _BOILERPLATE.search(line)]
    if not content_lines:
        return False

    # 目录/导航页：多数行是短标题 + 页码引导
    toc_like = sum(
        1
        for line in content_lines
        if _PAGE_REFERENCE.search(line)
        or (_visible_len(line) <= 12 and _BULLET_PREFIX.match(line))
    )
    if len(content_lines) >= 3 and toc_like / len(content_lines) >= 0.6:
        return False

    # 没有任何句子终止符或顿逗连接，基本是碎片
    joined = "\n".join(content_lines)
    if not _SENTENCE_END.search(joined) and "，" not in joined and "," not in joined:
        return False

    # 纯表格行且无完整句子
    table_lines = [
        line for line in content_lines if _TABLE_SEPARATOR.search(line)
    ]
    if (
        table_lines
        and len(table_lines) == len(content_lines)
        and not any(_SENTENCE_END.search(line) for line in content_lines)
    ):
        return False

    # 大部分行只是编号/格式标记
    marker_only = sum(
        1
        for line in content_lines
        if _BULLET_PREFIX.match(line) and _visible_len(line) <= 8
    )
    if len(content_lines) >= 4 and marker_only / len(content_lines) >= 0.7:
        return False

    return True
