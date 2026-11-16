from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import time
import re
import json
from pypdf import PdfReader
from html.parser import HTMLParser
import zipfile
from langchain_core.documents import Document


MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_PARSED_CHARACTERS = 20 * 1024 * 1024
MAX_PDF_PAGES = 10000
MAX_DOCX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_DOCX_PART_BYTES = 16 * 1024 * 1024


def _checked_source(path):
    from rag.errors import DocumentValidationError
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise DocumentValidationError('文档不存在或超过本地解析 64 MiB 上限。')
    return path


def _decoded_text(path, *, encoding='utf-8', autodetect_encoding=True):
    from rag.errors import DocumentValidationError
    raw = _checked_source(path).read_bytes()
    if raw.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        text = raw.decode('utf-32')
    elif raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = raw.decode('utf-16')
    else:
        try:
            text = raw.decode('utf-8-sig' if encoding == 'utf-8' else encoding)
        except UnicodeDecodeError:
            if not autodetect_encoding:
                raise
            import chardet
            detected = chardet.detect(raw)
            if not detected.get('encoding') or detected.get('confidence', 0) < .5:
                raise DocumentValidationError('无法可靠识别文本编码，请另存为 UTF-8 后重试。')
            text = raw.decode(detected['encoding'])
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if '\x00' in text or len(text) > MAX_PARSED_CHARACTERS:
        raise DocumentValidationError('文档包含二进制数据或解析文本超过 20 Mi 字符上限。')
    return text


class TextLoader:
    """Local text only; no URL fetching or executable document backends."""
    def __init__(self, file_path, encoding='utf-8', autodetect_encoding=True):
        self.path, self.encoding, self.autodetect_encoding = Path(file_path), encoding, autodetect_encoding

    def load(self):
        text = _decoded_text(self.path, encoding=self.encoding, autodetect_encoding=self.autodetect_encoding)
        return [Document(page_content=text, metadata={'source': str(self.path)})]


class UnstructuredMarkdownLoader(TextLoader):
    """Preserve Markdown code fences, headings and tables without plugins."""


class _VisibleHTML(HTMLParser):
    hidden_tags = {'script', 'style', 'noscript', 'template', 'svg'}
    block_tags = {'p', 'div', 'section', 'article', 'header', 'footer', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'tr', 'br', 'pre', 'blockquote'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden_tags:
            self.hidden += 1
        elif not self.hidden:
            if tag in self.block_tags:
                self.parts.append('\n')
            elif tag in {'td', 'th'}:
                self.parts.append('\t')

    def handle_endtag(self, tag):
        if tag in self.hidden_tags:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in self.block_tags:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class UnstructuredHTMLLoader(TextLoader):
    def load(self):
        parser = _VisibleHTML()
        parser.feed(_decoded_text(self.path))
        parser.close()
        text = re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', ''.join(parser.parts)).strip()
        return [Document(page_content=text, metadata={'source': str(self.path)})]


class Docx2txtLoader:
    def __init__(self, file_path):
        self.path = Path(file_path)

    def load(self):
        from rag.errors import DocumentValidationError
        import docx2txt
        _checked_source(self.path)
        with zipfile.ZipFile(self.path) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(entry.file_size for entry in entries) > MAX_DOCX_EXPANDED_BYTES:
                raise DocumentValidationError('DOCX 解压规模超过本地解析上限，请拆分后重试。')
            for entry in entries:
                if entry.file_size > MAX_DOCX_PART_BYTES or entry.flag_bits & 1:
                    raise DocumentValidationError('DOCX 含超大或加密部件，请另存为标准 DOCX 后重试。')
                if entry.filename.endswith('.xml'):
                    raw = archive.read(entry)
                    normalized = raw.replace(b'\x00', b'').upper()
                    if b'<!DOCTYPE' in normalized or b'<!ENTITY' in normalized:
                        raise DocumentValidationError('DOCX XML 不允许包含 DTD 或实体声明。')
        text = docx2txt.process(str(self.path)) or ''
        if len(text) > MAX_PARSED_CHARACTERS:
            raise DocumentValidationError('DOCX 解析文本超过 20 Mi 字符上限，请拆分后重试。')
        return [Document(page_content=text, metadata={'source': str(self.path)})]


class PyPDFLoader:
    def __init__(self, file_path):
        self.path = Path(file_path)

    def load(self):
        from rag.errors import DocumentValidationError
        _checked_source(self.path)
        with self.path.open('rb') as handle:
            if handle.read(5) != b'%PDF-':
                raise DocumentValidationError('文档解析失败，请检查 PDF 文件格式及文件是否损坏。')
        reader = PdfReader(str(self.path))
        if reader.is_encrypted:
            if not reader.decrypt(''):
                raise DocumentValidationError('PDF 已加密，请移除密码保护后重试。')
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentValidationError('PDF 超过本地解析 10000 页上限，请拆分后重试。')
        docs, count = [], 0
        for page, item in enumerate(reader.pages):
            text = item.extract_text() or ''
            count += len(text)
            if count > MAX_PARSED_CHARACTERS:
                raise DocumentValidationError('PDF 解析文本超过 20 Mi 字符上限，请拆分后重试。')
            docs.append(Document(page_content=text, metadata={'source': str(self.path), 'page': page, 'total_pages': len(reader.pages)}))
        return docs


SUPPORTED_EXTENSIONS = {".txt", ".pdf", ".md", ".docx", ".html", ".htm"}

# Bound only the OCR fallback; digitally generated text pages remain untouched.
# Render and recognize one page at a time, with no network or model calls.
PDF_OCR_MAX_PAGES = 40
PDF_OCR_MAX_SIDE = 3000
PDF_OCR_MAX_PAGE_PIXELS = 9_000_000
PDF_OCR_MAX_TOTAL_PIXELS = 180_000_000
PDF_OCR_STEP_TIMEOUT_SECONDS = 30
PDF_OCR_DOCUMENT_TIMEOUT_SECONDS = 180
PDF_OCR_MAX_TEXT_BYTES = 2_000_000
PDF_OCR_LANGUAGES = ("chi_sim", "eng")


def _ocr_pdf_pages(path, docs, *, force_ocr=False, ocr_pages=None):
    """Fill empty PDF pages using bounded local Poppler/Tesseract processes.

    Failure aborts this document rather than publishing a truncated OCR result.
    ``page`` stays zero based, matching PyPDFLoader and image evidence anchors.
    """
    from rag.errors import DocumentValidationError

    selected_pages = set(ocr_pages or [])
    if not selected_pages.issubset({doc.metadata.get('page', index) for index, doc in enumerate(docs)}):
        raise DocumentValidationError('指定的 OCR 页面超出 PDF 实际页数，请核对页码后重试。')
    empty = [(index, doc) for index, doc in enumerate(docs)
             if force_ocr or doc.metadata.get("page", index) in selected_pages or not doc.page_content.strip()]
    if not empty:
        return docs
    if len(empty) > PDF_OCR_MAX_PAGES:
        raise DocumentValidationError(
            f"PDF 含 {len(empty)} 个空文本页，超过本地 OCR 每文档 {PDF_OCR_MAX_PAGES} 页上限；请拆分后重试。"
        )
    poppler, tesseract = shutil.which("pdftoppm"), shutil.which("tesseract")
    if not poppler or not tesseract:
        raise DocumentValidationError(
            "PDF 含空文本页，需要本地 OCR；请安装 Poppler（pdftoppm）和 Tesseract，"
            "以及 chi_sim、eng 语言包。Linux 可安装 poppler-utils、tesseract-ocr、tesseract-ocr-chi-sim。"
        )
    try:
        from PIL import Image
    except ImportError as exc:
        raise DocumentValidationError("本地 PDF OCR 缺少 Pillow 图像依赖，请修复应用运行环境。", cause=exc) from exc

    deadline = time.monotonic() + PDF_OCR_DOCUMENT_TIMEOUT_SECONDS
    environment = {**os.environ, "OMP_THREAD_LIMIT": "1"}

    def run(command, stage):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DocumentValidationError(f"PDF 本地 OCR 超过整文档 {PDF_OCR_DOCUMENT_TIMEOUT_SECONDS} 秒上限，请拆分后重试。")
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=min(PDF_OCR_STEP_TIMEOUT_SECONDS, remaining), env=environment)
        except subprocess.TimeoutExpired as exc:
            raise DocumentValidationError(f"PDF 本地 OCR {stage}超时，请拆分或降低页面复杂度后重试。", cause=exc) from exc
        except OSError as exc:
            raise DocumentValidationError(f"PDF 本地 OCR 无法启动{stage}工具，请检查本地依赖及执行权限。", cause=exc) from exc
        if result.returncode:
            # Tool stderr may contain source paths or document content.
            raise DocumentValidationError(f"PDF 本地 OCR {stage}失败（退出码 {result.returncode}），请检查文档及本地 OCR 环境。")
        return result

    installed = set(run([tesseract, "--list-langs"], "语言检查").stdout.splitlines())
    missing = [language for language in PDF_OCR_LANGUAGES if language not in installed]
    if missing:
        raise DocumentValidationError(f"PDF 本地 OCR 缺少 Tesseract 语言包：{', '.join(missing)}；请安装后重试。")

    total_pixels = 0
    with tempfile.TemporaryDirectory(prefix="rag-pdf-ocr-") as directory:
        directory = Path(directory)
        for index, doc in empty:
            page = doc.metadata.get("page", index)
            if not isinstance(page, int) or isinstance(page, bool) or page < 0:
                page = index
            prefix = directory / "page"
            image_path = prefix.with_suffix(".png")
            text_path = prefix.with_suffix(".txt")
            run([poppler, "-q", "-f", str(page + 1), "-l", str(page + 1), "-singlefile", "-png",
                 "-r", "200", "-scale-to", str(PDF_OCR_MAX_SIDE), str(path.resolve()), str(prefix)],
                f"第 {page + 1} 页渲染")
            try:
                with Image.open(image_path) as image:
                    width, height = image.size
            except (OSError, ValueError) as exc:
                raise DocumentValidationError(f"PDF 本地 OCR 第 {page + 1} 页未生成有效图像。", cause=exc) from exc
            pixels = width * height
            total_pixels += pixels
            if max(width, height) > PDF_OCR_MAX_SIDE or pixels > PDF_OCR_MAX_PAGE_PIXELS or total_pixels > PDF_OCR_MAX_TOTAL_PIXELS:
                raise DocumentValidationError(
                    f"PDF 本地 OCR 像素超限：单页长边最多 {PDF_OCR_MAX_SIDE}、单页最多 {PDF_OCR_MAX_PAGE_PIXELS} 像素，"
                    f"文档累计最多 {PDF_OCR_MAX_TOTAL_PIXELS} 像素；请拆分后重试。"
                )
            run([tesseract, str(image_path), str(prefix), "-l", "+".join(PDF_OCR_LANGUAGES), "--psm", "3"],
                f"第 {page + 1} 页文字识别")
            try:
                if text_path.stat().st_size > PDF_OCR_MAX_TEXT_BYTES:
                    raise DocumentValidationError(f"PDF 本地 OCR 第 {page + 1} 页输出超过 {PDF_OCR_MAX_TEXT_BYTES} 字节上限。")
                text = text_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise DocumentValidationError(f"PDF 本地 OCR 第 {page + 1} 页未生成文字结果。", cause=exc) from exc
            doc.page_content = text
            doc.metadata.update(page=page, extraction_method="local_ocr", ocr_languages="+".join(PDF_OCR_LANGUAGES), ocr_recommended=False)
            image_path.unlink(missing_ok=True)
            text_path.unlink(missing_ok=True)
    if not any(doc.page_content.strip() for doc in docs):
        raise DocumentValidationError("PDF 空文本页已尝试本地 OCR，但未识别到可读文字；请检查扫描清晰度、语言和页面内容。")
    return docs


def _pdf_density_diagnostics(path, docs):
    """Flag sparse pages containing images; preserve native text until requested.

    A short title is not automatically OCR'd. The image signal distinguishes
    suspicious mixed scan pages from ordinary sparse digital pages.
    """
    image_pages = set()
    try:
        for index, page in enumerate(PdfReader(str(path)).pages):
            resources = page.get("/Resources")
            resources = resources.get_object() if resources else {}
            objects = resources.get("/XObject") or {}
            objects = objects.get_object()
            if any(item.get_object().get("/Subtype") == "/Image" for item in objects.values()):
                image_pages.add(index)
    except Exception:
        # Native loader remains authoritative; diagnostics cannot break parsing.
        pass
    for index, doc in enumerate(docs):
        chars = len(re.sub(r"\s", "", doc.page_content))
        page = doc.metadata.get("page", index)
        recommended = page in image_pages and 0 < chars < 100
        doc.metadata.update(native_text_characters=chars, ocr_recommended=recommended)
        if recommended:
            doc.metadata["parse_diagnostics"] = "sparse_text_with_images;review_or_force_ocr"


def _table_metadata(docs, path=None):
    """Retain explicit Markdown/HTML table rows without guessing PDF columns."""
    for doc in docs:
        tables = []
        lines = doc.page_content.splitlines()
        for index, line in enumerate(lines[:-1]):
            if "|" in line and re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1]):
                rows = [line]
                cursor = index + 2
                while cursor < len(lines) and "|" in lines[cursor]:
                    rows.append(lines[cursor])
                    cursor += 1
                tables.append({"format": "markdown", "rows": rows[:100], "start_line": index + 1})
        if tables:
            doc.metadata["table_structure"] = json.dumps(tables[:20], ensure_ascii=False)
        doc.metadata["parse_preview"] = doc.page_content[:500]
    if not docs or path is None or path.stat().st_size > 5_000_000:
        return
    tables = []
    try:
        if path.suffix.lower() == ".docx":
            import zipfile
            from xml.etree.ElementTree import fromstring
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > 5_000_000:
                    return
                root = fromstring(archive.read(info))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            for table in root.findall(".//w:tbl", ns)[:20]:
                rows = [["".join(cell.itertext()).strip() for cell in row.findall("w:tc", ns)]
                        for row in table.findall("w:tr", ns)[:100]]
                tables.append(rows)
        elif path.suffix.lower() in {".html", ".htm"}:
            from html.parser import HTMLParser
            class TableParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.table = None
                    self.row = None
                    self.cell = None
                def handle_starttag(self, tag, attrs):
                    if tag == "table": self.table = []
                    elif tag == "tr" and self.table is not None: self.row = []
                    elif tag in {"td", "th"} and self.row is not None: self.cell = []
                def handle_data(self, data):
                    if self.cell is not None: self.cell.append(data)
                def handle_endtag(self, tag):
                    if tag in {"td", "th"} and self.cell is not None:
                        self.row.append(" ".join(self.cell).strip())
                        self.cell = None
                    elif tag == "tr" and self.row is not None:
                        self.table.append(self.row[:30])
                        self.row = None
                    elif tag == "table" and self.table is not None:
                        if len(tables) < 20: tables.append(self.table[:100])
                        self.table = None
            TableParser().feed(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return
    rendered = []
    for rows in tables:
        if not rows or not rows[0]:
            continue
        escaped = [[cell.replace("|", "\\|").replace("\n", " ")[:1000] for cell in row] for row in rows]
        rendered.append("\n".join(["| " + " | ".join(escaped[0]) + " |",
            "| " + " | ".join("---" for _ in escaped[0]) + " |",
            *("| " + " | ".join(row) + " |" for row in escaped[1:])]))
    if rendered:
        docs[0].page_content += "\n\n结构化表格：\n" + "\n\n".join(rendered)
        docs[0].metadata["table_structure"] = json.dumps({"format": "markdown", "tables": rendered}, ensure_ascii=False)


def is_supported_document(path):
    path = Path(path)

    return (
        path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not path.name.startswith("~$")
    )


def load_document(file_path, *, force_ocr=False, ocr_pages=None):
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".txt":
        loader = TextLoader(
            str(path),
            encoding="utf-8",
            autodetect_encoding=True,
        )
    elif suffix == ".pdf":
        loader = PyPDFLoader(str(path))
    elif suffix == ".md":
        loader = UnstructuredMarkdownLoader(str(path))
    elif suffix == ".docx":
        loader = Docx2txtLoader(str(path))
    elif suffix in {".html", ".htm"}:
        loader = UnstructuredHTMLLoader(str(path))
    else:
        return []

    try:
        docs = loader.load()
    except Exception as exc:
        from rag.errors import DocumentValidationError
        if isinstance(exc, DocumentValidationError):
            raise
        raise DocumentValidationError('文档解析失败，请检查文件格式、加密状态及文件是否损坏。', cause=exc) from exc

    if suffix == ".pdf":
        # Keep OCR failures outside the legacy loader's catch-all so the task
        # records a useful diagnostic instead of a generic empty-content error.
        _pdf_density_diagnostics(path, docs)
        docs = _ocr_pdf_pages(path, docs, force_ocr=force_ocr, ocr_pages=ocr_pages)

    _table_metadata(docs, path)

    return [doc for doc in docs if doc.page_content.strip()]


def load_documents(doc_path):
    docs = []

    for path in Path(doc_path).rglob("*"):
        if not is_supported_document(path):
            continue

        docs.extend(load_document(path))

    return docs
