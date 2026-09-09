"""从富文本文档中识别代码块与正文段。

切分器（保证代码块不被切断）与文档代码图谱（把内联代码变成可检索符号）
共用这一份识别逻辑。纯文本处理，不依赖 Document 或数据库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

_FENCE_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})(?P<lang>[^\n`]*)$")
_PRE_OPEN_RE = re.compile(r"<pre\b[^>]*>", re.IGNORECASE)
_PRE_CLOSE_RE = re.compile(r"</pre\s*>", re.IGNORECASE)
_CODE_TAG_RE = re.compile(r"</?code\b[^>]*>", re.IGNORECASE)
_CLOSING_FENCE_RE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*$")

_CODE_HINT_RE = re.compile(
    r"(?:[;{}]\s*$)|(?:\b(?:def|class|function|func|fn|var|let|const|public|private|"
    r"return|import|from|if|for|while|switch|try|catch|select|insert|update|delete)\b)"
    r"|(?:=>)|(?://)|(?:/\*)|(?:#!)|(?::=)|(?:->)|(?:::)|(?:\(\))"
)

_LANGUAGE_ALIASES = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "node": "javascript",
    "ts": "typescript",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "yml": "yaml",
    "c++": "cpp",
    "c#": "csharp",
    "golang": "go",
    "rs": "rust",
}


def normalize_language(language: str | None) -> str | None:
    if not language:
        return None
    cleaned = language.strip().lower().split()[0] if language.strip() else ""
    if not cleaned:
        return None
    return _LANGUAGE_ALIASES.get(cleaned, cleaned)


@dataclass(frozen=True)
class Segment:
    """文本中的一个连续段。"""

    kind: str  # "code" | "text"
    text: str  # 段内容；code 段不含围栏/标签
    language: str | None = None
    fence: str | None = None  # "```" / "~~~" / "<pre>" / None 表示缩进代码块
    indent: str = ""  # 缩进代码块的公共缩进前缀
    start_line: int = 1  # 1-based，含
    end_line: int = 1  # 1-based，含

    @property
    def is_code(self) -> bool:
        return self.kind == "code"


def _is_closing_fence(line: str, fence: str) -> bool:
    match = _CLOSING_FENCE_RE.match(line)
    return bool(match and match.group("fence")[0] == fence[0])


def _looks_indented(line: str) -> bool:
    if not line.strip():
        return False
    if line[:1] not in {" ", "\t"}:
        return False
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip(" ")) >= 4


def _code_line_ratio(lines: list[str]) -> float:
    meaningful = [line for line in lines if line.strip()]
    if not meaningful:
        return 0.0
    hits = sum(1 for line in meaningful if _CODE_HINT_RE.search(line))
    return hits / len(meaningful)


def _flush_text(
    buffer: list[str],
    start_line: int,
    segments: list[Segment],
) -> None:
    if not buffer:
        return
    text = "\n".join(buffer)
    if text.strip():
        segments.append(
            Segment(
                kind="text",
                text=text,
                start_line=start_line,
                end_line=start_line + len(buffer) - 1,
            )
        )
    buffer.clear()


def extract_segments(
    text: str,
    *,
    min_block_lines: int = 3,
    include_indented: bool = True,
) -> list[Segment]:
    """把文本切成交替的正文段与代码段。"""
    lines = text.split("\n")
    total = len(lines)
    segments: list[Segment] = []
    buffer: list[str] = []
    buffer_start = 1
    index = 0

    while index < total:
        line = lines[index]

        fence_match = _FENCE_RE.match(line)
        if fence_match:
            _flush_text(buffer, buffer_start, segments)
            buffer_start = index + 1
            fence = fence_match.group("fence")
            language = normalize_language(fence_match.group("lang"))
            cursor = index + 1
            body: list[str] = []
            while cursor < total and not _is_closing_fence(lines[cursor], fence):
                body.append(lines[cursor])
                cursor += 1
            end = cursor if cursor < total else total - 1
            if body and any(item.strip() for item in body):
                segments.append(
                    Segment(
                        kind="code",
                        text="\n".join(body).rstrip(),
                        language=language,
                        fence=fence,
                        start_line=index + 1,
                        end_line=end + 1,
                    )
                )
            index = (cursor + 1) if cursor < total else total
            buffer_start = index + 1
            continue

        pre_match = _PRE_OPEN_RE.search(line)
        if pre_match:
            _flush_text(buffer, buffer_start, segments)
            buffer_start = index + 1
            cursor = index
            body = []
            while cursor < total:
                current = lines[cursor]
                if cursor == index:
                    current = current[pre_match.end():]
                close = _PRE_CLOSE_RE.search(current)
                if close:
                    body.append(current[: close.start()])
                    break
                body.append(current)
                cursor += 1
            end = cursor if cursor < total else total - 1
            cleaned = _CODE_TAG_RE.sub("", "\n".join(body))
            if cleaned.strip():
                segments.append(
                    Segment(
                        kind="code",
                        text=cleaned.strip("\n"),
                        language=None,
                        fence="<pre>",
                        start_line=index + 1,
                        end_line=end + 1,
                    )
                )
            index = (cursor + 1) if cursor < total else total
            buffer_start = index + 1
            continue

        if include_indented and _looks_indented(line):
            cursor = index
            body = []
            while cursor < total:
                if _looks_indented(lines[cursor]):
                    body.append(lines[cursor])
                    cursor += 1
                    continue
                if not lines[cursor].strip() and cursor + 1 < total and _looks_indented(lines[cursor + 1]):
                    body.append(lines[cursor])
                    cursor += 1
                    continue
                break
            meaningful = [item for item in body if item.strip()]
            if len(meaningful) >= min_block_lines and _code_line_ratio(body) >= 0.5:
                _flush_text(buffer, buffer_start, segments)
                buffer_start = index + 1
                indent = min(
                    (len(item.expandtabs(4)) - len(item.expandtabs(4).lstrip(" ")))
                    for item in meaningful
                )
                stripped = "\n".join(
                    item.expandtabs(4)[indent:] if item.strip() else ""
                    for item in body
                )
                segments.append(
                    Segment(
                        kind="code",
                        text=stripped.rstrip(),
                        language=None,
                        fence=None,
                        indent=" " * indent,
                        start_line=index + 1,
                        end_line=index + len(body),
                    )
                )
                index = cursor
                buffer_start = index + 1
                continue

        if not buffer:
            buffer_start = index + 1
        buffer.append(line)
        index += 1

    _flush_text(buffer, buffer_start, segments)
    return segments


def iter_code_segments(
    text: str,
    *,
    min_block_lines: int = 3,
    include_indented: bool = True,
) -> Iterator[Segment]:
    """只产出代码段。"""
    for segment in extract_segments(
        text,
        min_block_lines=min_block_lines,
        include_indented=include_indented,
    ):
        if segment.is_code:
            yield segment
