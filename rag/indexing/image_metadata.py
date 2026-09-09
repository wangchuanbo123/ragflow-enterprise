"""图片元数据在向量库与关系库之间的编解码。

SQLite 的 ``metadata_json`` 是 JSON 列，可以直接存 ``list[dict]``；
Chroma 的 metadata 只接受标量，因此写入向量库前必须序列化成字符串。
"""

from __future__ import annotations

import json
from typing import Any

IMAGE_METADATA_KEY = "images"


def encode_images_for_vector(images: Any) -> str | None:
    """把图片列表序列化为可写入向量库的字符串。"""
    if images is None:
        return None
    if isinstance(images, str):
        return images
    try:
        return json.dumps(images, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def decode_images(raw: Any) -> list[dict]:
    """把向量库读回的字符串（或已经是列表的值）还原成图片列表。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []
