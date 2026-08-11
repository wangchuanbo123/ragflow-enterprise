"""Language-aware parser that converts temporary ASTs into stable symbols."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from rag.code_graph.schemas import ParsedFile, ParsedRelation, ParsedSymbol


LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".pyi": "python",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".cs": "c_sharp",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".php": "php", ".rb": "ruby",
}

PARSER_VERSION = "code-parser-v1"


def _excerpt(lines: list[str], start: int, end: int, max_lines: int = 30) -> str:
    start_idx = max(0, start - 1)
    end_idx = min(len(lines), max(start_idx + 1, min(end, start + max_lines - 1)))
    return "\n".join(lines[start_idx:end_idx])[:6000]


def _attribute_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        left = _attribute_name(node.value)
        return f"{left}.{node.attr}" if left else node.attr
    if isinstance(node, ast.Call):
        return _attribute_name(node.func)
    if isinstance(node, ast.Subscript):
        return _attribute_name(node.value)
    return ""


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, path: str, source: str):
        self.path = path
        self.lines = source.splitlines()
        self.module_name = Path(path).with_suffix("").as_posix().replace("/", ".")
        self.file_key = f"file:{path}"
        self.symbols: list[ParsedSymbol] = [
            ParsedSymbol(
                local_key=self.file_key,
                kind="FILE",
                name=Path(path).name,
                qualified_name=self.module_name,
                start_line=1,
                end_line=max(1, len(self.lines)),
                source_excerpt=_excerpt(self.lines, 1, min(20, len(self.lines))),
            )
        ]
        self.relations: list[ParsedRelation] = []
        self.stack: list[tuple[str, str]] = [(self.file_key, self.module_name)]
        self.kind_by_key: dict[str, str] = {self.file_key: "FILE"}

    @property
    def current(self) -> tuple[str, str]:
        return self.stack[-1]

    def _add_symbol(self, node: ast.AST, kind: str, name: str, signature: str | None = None):
        parent_key, parent_qualified = self.current
        qualified = f"{parent_qualified}.{name}" if parent_qualified else name
        local_key = f"{self.path}::{qualified}"
        start = int(getattr(node, "lineno", 1))
        end = int(getattr(node, "end_lineno", start))
        self.symbols.append(ParsedSymbol(
            local_key=local_key,
            parent_local_key=parent_key,
            kind=kind,
            name=name,
            qualified_name=qualified,
            signature=signature,
            start_line=start,
            end_line=end,
            source_excerpt=_excerpt(self.lines, start, end),
        ))
        self.kind_by_key[local_key] = kind
        self.relations.append(ParsedRelation(
            subject_local_key=parent_key,
            target_qualified_name=qualified,
            relation_type="CONTAINS",
            start_line=start,
            end_line=start,
        ))
        return local_key, qualified

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str):
        try:
            signature = f"{node.name}({ast.unparse(node.args)})"
        except Exception:
            signature = node.name
        key, qualified = self._add_symbol(node, kind, node.name, signature)
        self.stack.append((key, qualified))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._visit_function(node, "METHOD" if self.kind_by_key.get(self.current[0]) == "CLASS" else "FUNCTION")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._visit_function(node, "ASYNC_METHOD" if self.kind_by_key.get(self.current[0]) == "CLASS" else "ASYNC_FUNCTION")

    def visit_ClassDef(self, node: ast.ClassDef):
        key, qualified = self._add_symbol(node, "CLASS", node.name, node.name)
        for base in node.bases:
            target = _attribute_name(base)
            if target:
                self.relations.append(ParsedRelation(
                    subject_local_key=key,
                    target_qualified_name=target,
                    relation_type="EXTENDS",
                    start_line=node.lineno,
                    end_line=node.lineno,
                ))
        self.stack.append((key, qualified))
        self.generic_visit(node)
        self.stack.pop()

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.relations.append(ParsedRelation(
                subject_local_key=self.current[0],
                target_qualified_name=alias.name,
                relation_type="IMPORTS",
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
            ))

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            target = f"{module}.{alias.name}".strip(".")
            self.relations.append(ParsedRelation(
                subject_local_key=self.current[0],
                target_qualified_name=target,
                relation_type="IMPORTS",
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
            ))

    def visit_Call(self, node: ast.Call):
        target = _attribute_name(node.func)
        if target:
            self.relations.append(ParsedRelation(
                subject_local_key=self.current[0],
                target_qualified_name=target,
                relation_type="CALLS",
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                confidence=0.9,
            ))
        self.generic_visit(node)


class CodeParser:
    """Parse source into a temporary AST and emit only durable symbol facts."""

    @staticmethod
    def detect_language(path: str | Path) -> str | None:
        return LANGUAGE_BY_SUFFIX.get(Path(path).suffix.lower())

    @classmethod
    def supported(cls, path: str | Path) -> bool:
        return cls.detect_language(path) is not None

    def parse(self, path: str, source: str) -> ParsedFile:
        language = self.detect_language(path)
        if language is None:
            return ParsedFile(path=path, language="unknown", parser_backend="none", errors=["unsupported language"])
        if language == "python":
            return self._parse_python(path, source)
        return self._parse_tree_sitter_or_regex(path, source, language)

    def _parse_python(self, path: str, source: str) -> ParsedFile:
        try:
            temporary_ast = ast.parse(source, filename=path, type_comments=True)
            visitor = _PythonVisitor(path, source)
            visitor.visit(temporary_ast)
            return ParsedFile(
                path=path,
                language="python",
                parser_backend="python_ast",
                symbols=visitor.symbols,
                relations=visitor.relations,
            )
        except SyntaxError as exc:
            fallback = self._parse_regex(path, source, "python")
            fallback.errors.append(f"SyntaxError line {exc.lineno}: {exc.msg}")
            return fallback

    def _parse_tree_sitter_or_regex(self, path: str, source: str, language: str) -> ParsedFile:
        errors: list[str] = []
        backend = "regex"
        try:
            from tree_sitter_language_pack import get_parser

            parser = get_parser(language)
            temporary_ast = parser.parse(source.encode("utf-8", errors="replace"))
            backend = "tree_sitter"
            if temporary_ast.root_node.has_error:
                errors.append("tree-sitter reported syntax errors; recovered named nodes were used")
        except Exception as exc:
            errors.append(f"tree-sitter unavailable: {type(exc).__name__}")
        parsed = self._parse_regex(path, source, language)
        parsed.parser_backend = backend
        parsed.errors.extend(errors)
        return parsed

    def _parse_regex(self, path: str, source: str, language: str) -> ParsedFile:
        lines = source.splitlines()
        module = Path(path).with_suffix("").as_posix().replace("/", ".")
        file_key = f"file:{path}"
        symbols = [ParsedSymbol(
            local_key=file_key,
            kind="FILE",
            name=Path(path).name,
            qualified_name=module,
            start_line=1,
            end_line=max(1, len(lines)),
            source_excerpt=_excerpt(lines, 1, min(20, len(lines))),
        )]
        relations: list[ParsedRelation] = []
        declaration_patterns = [
            ("CLASS", re.compile(r"\b(?:class|interface|struct|trait|enum)\s+([A-Za-z_$][\w$]*)")),
            ("FUNCTION", re.compile(r"\b(?:def|func|function|fn)\s+([A-Za-z_$][\w$]*)\s*\(")),
            ("METHOD", re.compile(r"\b(?:public|private|protected|static|async|final|override|virtual|suspend|export\s+)?\s*(?:[\w<>\[\],.?]+\s+)?([A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*(?:\{|=>)")),
        ]
        declared_lines: set[int] = set()
        for line_no, line in enumerate(lines, 1):
            stripped = line.strip()
            for kind, pattern in declaration_patterns:
                match = pattern.search(stripped)
                if not match:
                    continue
                name = match.group(1)
                if name in {"if", "for", "while", "switch", "catch", "return", "new"}:
                    continue
                qualified = f"{module}.{name}"
                key = f"{path}::{qualified}:{line_no}"
                symbols.append(ParsedSymbol(
                    local_key=key,
                    parent_local_key=file_key,
                    kind=kind,
                    name=name,
                    qualified_name=qualified,
                    signature=stripped[:500],
                    start_line=line_no,
                    end_line=line_no,
                    source_excerpt=_excerpt(lines, line_no, min(len(lines), line_no + 12)),
                ))
                relations.append(ParsedRelation(
                    subject_local_key=file_key,
                    target_qualified_name=qualified,
                    relation_type="CONTAINS",
                    start_line=line_no,
                    end_line=line_no,
                ))
                declared_lines.add(line_no)
                break

            import_match = re.search(r"(?:import|require\s*\(|from)\s*[('\"]?([\w@./:-]+)", stripped)
            if import_match:
                relations.append(ParsedRelation(
                    subject_local_key=file_key,
                    target_qualified_name=import_match.group(1),
                    relation_type="IMPORTS",
                    start_line=line_no,
                    end_line=line_no,
                    confidence=0.9,
                ))
            if line_no not in declared_lines:
                for call in re.finditer(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(", stripped):
                    target = call.group(1)
                    if target.split(".")[-1] in {"if", "for", "while", "switch", "catch", "return", "new"}:
                        continue
                    relations.append(ParsedRelation(
                        subject_local_key=file_key,
                        target_qualified_name=target,
                        relation_type="CALLS",
                        start_line=line_no,
                        end_line=line_no,
                        confidence=0.65,
                    ))
        return ParsedFile(path=path, language=language, parser_backend="regex", symbols=symbols, relations=relations)
