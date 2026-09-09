"""共享的 token 计数工具。

切分器、上下文构建器都需要按 token 估算长度，集中在这里以避免模块循环导入。
"""

from __future__ import annotations

import tiktoken

_ENCODER: tiktoken.Encoding | None = None
_ENCODER_LOADED = False


def _get_encoder() -> tiktoken.Encoding | None:
    global _ENCODER, _ENCODER_LOADED
    if not _ENCODER_LOADED:
        _ENCODER_LOADED = True
        try:
            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODER = None
    return _ENCODER


def count_tokens(text: str) -> int:
    """估算文本的 token 数；tiktoken 不可用时按 2 字符 ≈ 1 token 兜底。"""
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 2)
