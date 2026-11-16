"""Local PDF OCR fallback, bounded resources and preserved evidence pages."""
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest
from langchain_core.documents import Document
from PIL import Image

from rag.errors import DocumentValidationError
from rag.loaders import document_loader as loader


def prepare(monkeypatch, tmp_path, contents):
    path = tmp_path / "mixed.pdf"
    path.write_bytes(b"synthetic fixture")
    documents = [Document(page_content=text, metadata={"source": str(path), "page": index})
                 for index, text in enumerate(contents)]
    monkeypatch.setattr(loader, "PyPDFLoader", lambda _path: SimpleNamespace(load=lambda: documents))
    monkeypatch.setattr(loader.shutil, "which", lambda tool: "/local/" + tool)
    return path, documents


def fake_tools(monkeypatch, *, text="SCANNED LOCAL MARKER", size=(600, 400), languages="chi_sim\neng\n"):
    calls = []
    images = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == "--list-langs":
            return SimpleNamespace(returncode=0, stdout=languages)
        if command[0].endswith("pdftoppm"):
            image = Path(command[-1]).with_suffix(".png")
            Image.new("RGB", size, "white").save(image)
            images.append(image)
        else:
            Path(command[2]).with_suffix(".txt").write_text(text, encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr(loader.subprocess, "run", run)
    return calls, images


def test_text_pages_do_not_require_or_invoke_ocr(monkeypatch, tmp_path):
    path, documents = prepare(monkeypatch, tmp_path, ["native text", "原文优先"])
    monkeypatch.setattr(loader.shutil, "which", lambda tool: pytest.fail("text pages do not need OCR tools"))
    assert loader.load_document(path) == documents
    assert all("extraction_method" not in item.metadata for item in documents)


def test_selected_ocr_pages_must_exist_in_the_pdf(monkeypatch, tmp_path):
    path, _ = prepare(monkeypatch, tmp_path, ['native text'])
    monkeypatch.setattr(loader.shutil, 'which', lambda _tool: pytest.fail('Invalid page should be rejected before OCR starts'))
    with pytest.raises(DocumentValidationError, match='超出 PDF 实际页数'):
        loader.load_document(path, ocr_pages=[1])


def test_only_empty_pages_are_ocrd_and_original_page_anchors_are_preserved(monkeypatch, tmp_path):
    path, documents = prepare(monkeypatch, tmp_path, ["native text", " \n", "another original page"])
    calls, images = fake_tools(monkeypatch)
    result = loader.load_document(path)
    assert [item.page_content for item in result] == ["native text", "SCANNED LOCAL MARKER", "another original page"]
    assert [item.metadata["page"] for item in result] == [0, 1, 2]
    assert all(item.metadata["source"] == str(path) for item in result)
    assert "extraction_method" not in documents[0].metadata
    assert result[1].metadata["extraction_method"] == "local_ocr"
    render = next(command for command, _ in calls if command[0].endswith("pdftoppm"))
    assert render[render.index("-f") + 1] == "2"
    assert render[render.index("-l") + 1] == "2"
    assert all(options["env"]["OMP_THREAD_LIMIT"] == "1" and options["timeout"] <= 30 for _, options in calls)
    assert all(not image.parent.exists() for image in images)


def test_page_count_is_rejected_before_starting_any_tool(monkeypatch, tmp_path):
    path, _ = prepare(monkeypatch, tmp_path, [""] * (loader.PDF_OCR_MAX_PAGES + 1))
    monkeypatch.setattr(loader.shutil, "which", lambda tool: pytest.fail("must reject before dependency lookup"))
    with pytest.raises(DocumentValidationError, match="每文档 40 页上限"):
        loader.load_document(path)


def test_missing_ocr_binaries_are_a_clear_document_error(monkeypatch, tmp_path):
    path, _ = prepare(monkeypatch, tmp_path, [""])
    monkeypatch.setattr(loader.shutil, "which", lambda tool: None)
    with pytest.raises(DocumentValidationError, match="poppler-utils、tesseract-ocr、tesseract-ocr-chi-sim"):
        loader.load_document(path)


def test_missing_language_pack_is_not_silently_ignored(monkeypatch, tmp_path):
    path, _ = prepare(monkeypatch, tmp_path, [""])
    fake_tools(monkeypatch, languages="eng\n")
    with pytest.raises(DocumentValidationError, match="缺少 Tesseract 语言包：chi_sim"):
        loader.load_document(path)


def test_rendered_image_is_rejected_above_pixel_budget_and_cleaned(monkeypatch, tmp_path):
    path, _ = prepare(monkeypatch, tmp_path, [""])
    calls, images = fake_tools(monkeypatch, size=(20, 20))
    monkeypatch.setattr(loader, "PDF_OCR_MAX_PAGE_PIXELS", 399)
    with pytest.raises(DocumentValidationError, match="像素超限"):
        loader.load_document(path)
    assert len(calls) == 2  # language check + render, no recognition
    assert all(not image.parent.exists() for image in images)


def test_cumulative_pixel_budget_aborts_instead_of_publishing_partial_ocr(monkeypatch, tmp_path):
    path, _ = prepare(monkeypatch, tmp_path, ["", ""])
    calls, images = fake_tools(monkeypatch, size=(20, 20))
    monkeypatch.setattr(loader, "PDF_OCR_MAX_TOTAL_PIXELS", 600)
    with pytest.raises(DocumentValidationError, match="累计最多 600"):
        loader.load_document(path)
    assert sum(command[0].endswith("tesseract") and command[1] != "--list-langs" for command, _ in calls) == 1
    assert all(not image.parent.exists() for image in images)


def test_timeout_surfaces_a_clear_diagnostic(monkeypatch, tmp_path):
    path, _ = prepare(monkeypatch, tmp_path, [""])
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(loader.subprocess, "run", run)
    with pytest.raises(DocumentValidationError, match="语言检查超时"):
        loader.load_document(path)


def test_tool_failure_does_not_expose_raw_stderr(monkeypatch, tmp_path):
    path, _ = prepare(monkeypatch, tmp_path, [""])
    monkeypatch.setattr(loader.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr="PRIVATE SOURCE CONTENT"))
    with pytest.raises(DocumentValidationError, match="退出码 2") as error:
        loader.load_document(path)
    assert "PRIVATE SOURCE CONTENT" not in str(error.value)


def test_empty_recognition_explains_unreadable_scan(monkeypatch, tmp_path):
    path, _ = prepare(monkeypatch, tmp_path, [""])
    fake_tools(monkeypatch, text=" \n")
    with pytest.raises(DocumentValidationError, match="未识别到可读文字"):
        loader.load_document(path)


def test_ocr_text_output_is_bounded(monkeypatch, tmp_path):
    path, _ = prepare(monkeypatch, tmp_path, [""])
    fake_tools(monkeypatch, text="X" * 30)
    monkeypatch.setattr(loader, "PDF_OCR_MAX_TEXT_BYTES", 20)
    with pytest.raises(DocumentValidationError, match="超过 20 字节上限"):
        loader.load_document(path)
