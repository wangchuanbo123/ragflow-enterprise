"""从文档中提取图片，并把图片引用以占位符形式锚定到文本。

设计要点：

1. 只使用标准库与已装依赖（pypdf 随 PyPDFLoader 引入），不新增第三方包。
2. 不传 ``ImageSink`` 时本模块完全不参与，文档加载行为与改造前一致。
3. 图片以 ``[[img-N]]`` 占位符写进 ``page_content``，随切片一起落到具体 chunk；
   ``chunk.metadata["images"]`` 只保留真正落在该 chunk 内的图片（由 chunker 过滤）。
"""

from __future__ import annotations

import base64
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

from langchain_core.documents import Document

IMAGE_PLACEHOLDER_PATTERN = re.compile(r"\[\[img-(\d+)\]\]")

_MAGIC_MIME = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)

_SUFFIX_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".ico": "image/vnd.microsoft.icon",
    ".emf": "image/emf",
    ".wmf": "image/wmf",
}

_SUFFIX_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
}

_MARKDOWN_IMAGE = re.compile(
    r"!\[([^\]]*)\]\(\s*<?([^)\s]+)>?(?:\s+\"[^\"]*\")?\s*\)"
)


class ImageSink(Protocol):
    """把图片字节持久化到对象存储，并返回可访问的 URL。"""

    def store(self, *, key: str, data: bytes, mime_type: str) -> str | None:
        """返回 None 表示放弃该图片（例如存储不可用）。"""


@dataclass
class ExtractedImage:
    """一份从文档中抽取出来的图片。"""

    index: int
    filename: str
    mime_type: str
    caption: str
    data: bytes | None = None
    url: str | None = None
    page: int | None = None
    width: int | None = None
    height: int | None = None
    position: str = "appendix"

    @property
    def marker(self) -> str:
        return f"img-{self.index}"

    @property
    def placeholder(self) -> str:
        return f"[[{self.marker}]]"

    def to_metadata(self, url: str) -> dict:
        return {
            "marker": self.marker,
            "url": url,
            "caption": self.caption,
            "mime_type": self.mime_type,
            "page": self.page,
            "width": self.width,
            "height": self.height,
            "filename": self.filename,
        }

    def anchor_line(self) -> str:
        caption = self.caption.strip()
        return f"{self.placeholder} {caption}" if caption else self.placeholder


def detect_mime(data: bytes, filename: str | None = None) -> str:
    for prefix, mime in _MAGIC_MIME:
        if data.startswith(prefix):
            if mime == "image/tiff":
                return mime
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix in _SUFFIX_MIME:
            return _SUFFIX_MIME[suffix]
    if data.lstrip()[:5] in {b"<svg ", b"<?xml"}:
        return "image/svg+xml"
    return "application/octet-stream"


def _suffix_for(mime_type: str, filename: str | None = None) -> str:
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix:
            return suffix
    return _SUFFIX_BY_MIME.get(mime_type, ".bin")


def _parse_data_uri(value: str) -> tuple[bytes, str] | None:
    lowered = value[:32].lower()
    if not lowered.startswith("data:"):
        return None
    header, _, payload = value.partition(",")
    meta = header[len("data:"):]
    mime_type = meta.split(";")[0].strip() or "application/octet-stream"
    try:
        if ";base64" in meta.lower():
            return base64.b64decode(payload, validate=False), mime_type
        return unquote(payload).encode("utf-8"), mime_type
    except Exception:
        return None


def _read_local_reference(reference: str, base_dir: Path) -> bytes | None:
    """解析指向本地文件的图片引用；远程地址返回 None。"""
    parsed = urlparse(reference)
    if parsed.scheme in {"http", "https"}:
        return None
    if parsed.scheme == "data":
        return None
    candidate = unquote(parsed.path or reference)
    if not candidate:
        return None
    path = Path(candidate)
    if not path.is_absolute():
        path = base_dir / path
    try:
        if path.is_file():
            return path.read_bytes()
    except OSError:
        return None
    return None


class _ImgTagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if str(tag).lower() != "img":
            return
        attr_map = {str(k).lower(): (v or "") for k, v in attrs}
        src = attr_map.get("src", "").strip()
        if src:
            self.images.append((attr_map.get("alt", "").strip(), src))


def _extract_pdf_images(path: Path, limit: int, max_bytes: int) -> list[ExtractedImage]:
    images: list[ExtractedImage] = []
    try:
        from pypdf import PdfReader
    except Exception:
        return images

    try:
        reader = PdfReader(str(path))
    except Exception:
        return images

    for page_no, page in enumerate(reader.pages, start=1):
        if len(images) >= limit:
            break
        try:
            page_images = list(page.images)
        except Exception:
            continue
        for raw in page_images:
            if len(images) >= limit:
                break
            try:
                data = raw.data
            except Exception:
                continue
            if not data or len(data) > max_bytes:
                continue
            name = getattr(raw, "name", "") or f"image_{len(images) + 1}"
            images.append(
                ExtractedImage(
                    index=len(images) + 1,
                    filename=Path(str(name)).name,
                    mime_type=detect_mime(data, str(name)),
                    caption="",
                    data=data,
                    page=page_no,
                    position="page",
                )
            )
    return images


def _extract_docx_images(path: Path, limit: int, max_bytes: int) -> list[ExtractedImage]:
    images: list[ExtractedImage] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(
                name
                for name in archive.namelist()
                if name.startswith("word/media/") and not name.endswith("/")
            )
            for name in names:
                if len(images) >= limit:
                    break
                try:
                    data = archive.read(name)
                except Exception:
                    continue
                if not data or len(data) > max_bytes:
                    continue
                images.append(
                    ExtractedImage(
                        index=len(images) + 1,
                        filename=Path(name).name,
                        mime_type=detect_mime(data, name),
                        caption=Path(name).stem,
                        data=data,
                        position="appendix",
                    )
                )
    except Exception:
        return images
    return images


def _extract_html_images(path: Path, limit: int, max_bytes: int) -> list[ExtractedImage]:
    images: list[ExtractedImage] = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return images

    collector = _ImgTagCollector()
    try:
        collector.feed(raw)
    except Exception:
        return images

    base_dir = path.parent
    for alt, src in collector.images:
        if len(images) >= limit:
            break
        data: bytes | None = None
        url: str | None = None
        parsed_data = _parse_data_uri(src)
        if parsed_data is not None:
            data, mime_from_uri = parsed_data
            mime_type = mime_from_uri
        else:
            if urlparse(src).scheme in {"http", "https"}:
                url = src
                mime_type = detect_mime(b"", src)
            else:
                data = _read_local_reference(src, base_dir)
                if data is None:
                    url = src
                    mime_type = detect_mime(b"", src)
                else:
                    mime_type = detect_mime(data, src)
        if data is not None and (not data or len(data) > max_bytes):
            continue
        images.append(
            ExtractedImage(
                index=len(images) + 1,
                filename=_filename_for(images, mime_type, src),
                mime_type=mime_type,
                caption=alt,
                data=data,
                url=url if data is None else None,
                position="appendix",
            )
        )
    return images


def _extract_markdown_images(path: Path, limit: int, max_bytes: int) -> list[ExtractedImage]:
    images: list[ExtractedImage] = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return images

    base_dir = path.parent
    for match in _MARKDOWN_IMAGE.finditer(raw):
        if len(images) >= limit:
            break
        alt, src = match.group(1).strip(), match.group(2).strip()
        if not src:
            continue
        data: bytes | None = None
        url: str | None = None
        parsed_data = _parse_data_uri(src)
        if parsed_data is not None:
            data, mime_type = parsed_data
        elif urlparse(src).scheme in {"http", "https"}:
            url = src
            mime_type = detect_mime(b"", src)
        else:
            data = _read_local_reference(src, base_dir)
            if data is None:
                url = src
                mime_type = detect_mime(b"", src)
            else:
                mime_type = detect_mime(data, src)
        if data is not None and (not data or len(data) > max_bytes):
            continue
        images.append(
            ExtractedImage(
                index=len(images) + 1,
                filename=_filename_for(images, mime_type, src),
                mime_type=mime_type,
                caption=alt,
                data=data,
                url=url if data is None else None,
                position="appendix",
            )
        )
    return images


def _filename_for(images: list[ExtractedImage], mime_type: str, reference: str) -> str:
    suffix = _suffix_for(mime_type, reference.split("?")[0])
    return f"image_{len(images) + 1}{suffix}"


_EXTRACTORS = {
    ".pdf": _extract_pdf_images,
    ".docx": _extract_docx_images,
    ".html": _extract_html_images,
    ".htm": _extract_html_images,
    ".md": _extract_markdown_images,
}


def extract_document_images(
    file_path: str | Path,
    *,
    limit: int = 40,
    max_bytes: int = 8 * 1024 * 1024,
) -> list[ExtractedImage]:
    """从文档中提取图片；失败或格式不支持时返回空列表。"""
    path = Path(file_path)
    extractor = _EXTRACTORS.get(path.suffix.lower())
    if extractor is None:
        return []
    try:
        images = extractor(path, limit, max_bytes)
    except Exception:
        return []
    for position, image in enumerate(images, start=1):
        image.index = position
    return images


def attach_document_images(
    docs: list[Document],
    images: list[ExtractedImage],
    sink: ImageSink | None,
    *,
    object_prefix: str,
) -> list[ExtractedImage]:
    """把图片写入对象存储，并把占位符锚定到承载它的 Document。

    返回成功写入对象存储的图片；未落盘的图片不会写进 metadata。
    """
    if not docs or not images or sink is None:
        return []

    stored: list[ExtractedImage] = []
    for image in images:
        if image.data is None:
            # 远程图片没有字节可存，直接沿用原 URL。
            stored.append(image)
            continue
        suffix = _suffix_for(image.mime_type, image.filename)
        key = f"{object_prefix.rstrip('/')}/images/{image.marker}{suffix}"
        try:
            url = sink.store(key=key, data=image.data, mime_type=image.mime_type)
        except Exception:
            url = None
        if not url:
            continue
        image.url = url
        stored.append(image)

    if not stored:
        return []

    by_page: dict[int, list[ExtractedImage]] = {}
    appendix: list[ExtractedImage] = []
    for image in stored:
        if image.position == "page" and image.page is not None:
            by_page.setdefault(image.page, []).append(image)
        else:
            appendix.append(image)

    # PyPDFLoader 的 page 元数据是 0-based，图片页码是 1-based。
    for doc in docs:
        page_value = doc.metadata.get("page")
        if page_value is None:
            continue
        try:
            page_images = by_page.get(int(page_value) + 1)
        except (TypeError, ValueError):
            page_images = None
        if not page_images:
            continue
        doc.page_content = _append_anchor(doc.page_content, page_images)
        doc.metadata["images"] = [item.to_metadata(item.url or "") for item in page_images]

    if appendix:
        target = docs[-1]
        target.page_content = _append_anchor(target.page_content, appendix)
        existing = list(target.metadata.get("images") or [])
        existing.extend(item.to_metadata(item.url or "") for item in appendix)
        target.metadata["images"] = existing

    return stored


def _append_anchor(content: str, images: list[ExtractedImage]) -> str:
    lines = [content.rstrip(), *[item.anchor_line() for item in images]]
    return "\n".join(part for part in lines if part) + "\n"
