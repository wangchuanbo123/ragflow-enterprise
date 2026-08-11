from __future__ import annotations

from pydantic import BaseModel, Field


class ParsedSymbol(BaseModel):
    local_key: str
    parent_local_key: str | None = None
    kind: str
    name: str
    qualified_name: str
    signature: str | None = None
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=1, ge=1)
    source_excerpt: str | None = None
    metadata: dict = Field(default_factory=dict)


class ParsedRelation(BaseModel):
    subject_local_key: str
    target_qualified_name: str
    relation_type: str
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=1, ge=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: dict = Field(default_factory=dict)


class ParsedFile(BaseModel):
    path: str
    language: str
    parser_backend: str
    symbols: list[ParsedSymbol] = Field(default_factory=list)
    relations: list[ParsedRelation] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
