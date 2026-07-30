"""实体名称归一化。"""

from __future__ import annotations

import re
import unicodedata


def normalize_name(name: str) -> str:
    """对实体名称执行归一化。

    - Unicode NFKC 归一化
    - 去除首尾空白和无意义标点
    - 合并连续空格
    - 英文字母转小写，中文保持原样
    """
    if not name:
        return ""

    text = unicodedata.normalize("NFKC", name)
    text = text.strip()
    text = re.sub(r"[\s\u3000]+", " ", text)
    text = text.strip("""""""''!?。，.,;:、""")

    result = []
    for ch in text:
        if "A" <= ch <= "Z":
            result.append(ch.lower())
        else:
            result.append(ch)
    return "".join(result)


def normalize_alias(alias: str) -> str:
    """别名归一化，规则同实体名称。"""
    return normalize_name(alias)
