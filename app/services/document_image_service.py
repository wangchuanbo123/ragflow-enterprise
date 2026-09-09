"""把文档中抽取出的图片写入对象存储，并给出可访问的 URL。"""

from __future__ import annotations

from typing import Protocol

from app.core.config import IMAGE_URL_PREFIX


class ObjectStoreLike(Protocol):
    def put_bytes(self, key: str, data: bytes):
        """返回一个带 .key 的对象引用。"""


class ObjectStoreImageSink:
    """``ImageSink`` 实现：写入对象存储，返回 API 可访问的相对 URL。"""

    def __init__(
        self,
        object_store: ObjectStoreLike,
        url_prefix: str | None = None,
    ):
        self.object_store = object_store
        self.url_prefix = (url_prefix or IMAGE_URL_PREFIX).rstrip("/")

    def store(self, *, key: str, data: bytes, mime_type: str) -> str | None:
        del mime_type
        reference = self.object_store.put_bytes(key.lstrip("/"), data)
        object_key = getattr(reference, "key", None) or key
        return f"{self.url_prefix}/{str(object_key).lstrip('/')}"
