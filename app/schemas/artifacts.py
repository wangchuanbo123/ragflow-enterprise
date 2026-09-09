"""Structured answer artifacts shared by RAG and conversation APIs."""

from typing import Literal

from pydantic import BaseModel


class SourceImage(BaseModel):
    marker: str = ""
    url: str = ""
    caption: str = ""
    mime_type: str = "application/octet-stream"
    page: int | str | None = None
    width: int | None = None
    height: int | None = None
    filename: str = ""
    knowledge_base_id: str = ""
    knowledge_base_name: str = ""
    document_id: str = ""
    chunk_id: str = ""
    citation_id: str = ""


class CodeSnippet(BaseModel):
    content: str
    language: str = ""
    path: str = ""
    start_line: int | None = None
    end_line: int | None = None
    line_scope: Literal["file", "chunk"] = "chunk"
    revision: str = ""
    origin: Literal["document", "repository", "generated"] = "document"
    is_verbatim: bool = True
    symbol_id: str = ""
    qualified_name: str = ""
    chunk_index: int | None = None
    knowledge_base_id: str = ""
    knowledge_base_name: str = ""
    document_id: str = ""
    chunk_id: str = ""
    citation_id: str = ""
