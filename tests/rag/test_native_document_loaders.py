"""Native parsers retain text/evidence and reject dangerous containers."""
from pathlib import Path
import zipfile

import pytest

from rag.errors import DocumentValidationError
from rag.loaders import document_loader as loader


def test_utf8_markdown_retains_code_fences_tables_and_source(tmp_path):
    path=tmp_path/'设计.md'
    content='# 章节\n\n```python\nprint("你好")\n```\n\n| 名称 | 值 |\n| --- | --- |\n| A | 3 |'
    path.write_text(content,encoding='utf-8')
    docs=loader.load_document(path)
    assert docs[0].page_content==content and docs[0].metadata['source']==str(path)
    assert 'table_structure' in docs[0].metadata and docs[0].metadata['parse_preview'].startswith('# 章节')


@pytest.mark.parametrize('encoding',['utf-8-sig','utf-16','utf-32','gb18030'])
def test_native_text_supports_common_document_encodings(tmp_path,encoding):
    path=tmp_path/'文档.txt'
    text='这是用于核验编码的中文技术说明，保留原始文档中的企业知识与业务规范。' * 4
    path.write_bytes(text.encode(encoding))
    assert loader.load_document(path)[0].page_content==text


def test_html_uses_visible_local_text_and_retains_table_rows_without_fetching(tmp_path,monkeypatch):
    path=tmp_path/'manual.html'
    path.write_text('<html><style>SECRET STYLE</style><script>SECRET SCRIPT</script><body><h1>标题</h1><p>Visible &amp; escaped</p><img src="https://external.invalid/image"><table><tr><th>项目</th><th>数额</th></tr><tr><td>预算</td><td>8000</td></tr></table></body></html>',encoding='utf-8')
    import httpx
    monkeypatch.setattr(httpx,'get',lambda *_args,**_kwargs:pytest.fail('HTML parsing must not fetch URLs'))
    doc=loader.load_document(path)[0]
    assert '标题' in doc.page_content and 'Visible & escaped' in doc.page_content
    assert 'SECRET' not in doc.page_content
    assert '| 项目 | 数额 |' in doc.page_content and '| 预算 | 8000 |' in doc.page_content


def write_docx(path,body):
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('word/document.xml',body)


def test_native_docx_reads_paragraphs_and_structured_tables(tmp_path):
    path=tmp_path/'manual.docx'
    write_docx(path,'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>中文正文</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>名称</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>金额</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>合同</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>8000</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
    doc=loader.load_document(path)[0]
    assert '中文正文' in doc.page_content and '| 合同 | 8000 |' in doc.page_content
    assert 'table_structure' in doc.metadata


@pytest.mark.parametrize('encoding',['utf-8','utf-16'])
def test_docx_rejects_xml_entity_declarations_in_supported_xml_encodings(tmp_path,encoding):
    path=tmp_path/'entity.docx'
    write_docx(path,'<!DOCTYPE a [<!ENTITY secret "unsafe">]><a>&secret;</a>'.encode(encoding))
    with pytest.raises(DocumentValidationError,match='DTD'):
        loader.load_document(path)


def test_docx_expansion_is_bounded_before_parsing(tmp_path,monkeypatch):
    path=tmp_path/'oversized.docx'
    write_docx(path,b'<doc>'+b'x'*500+b'</doc>')
    monkeypatch.setattr(loader,'MAX_DOCX_EXPANDED_BYTES',100)
    with pytest.raises(DocumentValidationError,match='解压规模'):
        loader.load_document(path)


def test_native_pdf_keeps_each_page_and_zero_based_coordinates(tmp_path,monkeypatch):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    path=tmp_path/'pages.pdf'
    writer=PdfWriter()
    for text in ['FIRST PAGE EVIDENCE','SECOND PAGE EVIDENCE']:
        page=writer.add_blank_page(width=400,height=400)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(f'BT /F1 12 Tf 10 100 Td ({text}) Tj ET'.encode())
        page[NameObject('/Contents')]=writer._add_object(stream)
    with path.open('wb') as handle:writer.write(handle)
    monkeypatch.setattr(loader.shutil,'which',lambda _name:pytest.fail('Native PDF needs no OCR'))
    docs=loader.load_document(path)
    assert [doc.metadata['page'] for doc in docs]==[0,1]
    assert 'FIRST PAGE EVIDENCE' in docs[0].page_content and 'SECOND PAGE EVIDENCE' in docs[1].page_content


def test_encrypted_and_corrupt_pdf_produce_actionable_safe_errors(tmp_path):
    from pypdf import PdfWriter
    encrypted=tmp_path/'encrypted.pdf'
    writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.encrypt('private-password')
    with encrypted.open('wb') as handle:writer.write(handle)
    with pytest.raises(DocumentValidationError,match='PDF 已加密'):
        loader.load_document(encrypted)
    corrupt=tmp_path/'corrupt.pdf';corrupt.write_bytes(b'SECRET RAW DOCUMENT BYTES')
    with pytest.raises(DocumentValidationError,match='文档解析失败') as error:
        loader.load_document(corrupt)
    assert 'SECRET' not in str(error.value)
