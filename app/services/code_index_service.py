"""Build versioned code symbol/call/dependency graphs from source snapshots."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import (
    CODE_MAX_FILE_SIZE_MB,
    CODE_PARSE_WORKERS,
    CODE_SNAPSHOT_RETENTION,
    DOCUMENT_CODE_MAX_BLOCKS,
    DOCUMENT_CODE_MIN_BLOCK_LINES,
)
from app.core.config import INDEX_JOB_MAX_ATTEMPTS
from app.models.code_graph import CodeFile, CodeRelation, CodeSnapshot, CodeSymbol
from app.models.index_job import IndexJob
from app.repositories.code_graph_repository import CodeGraphRepository
from app.repositories.index_job_repository import IndexJobRepository, signal_persisted_job
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag.code_graph.document_source import (
    document_code_parser_version,
    extract_document_code,
    manifest_for_document_chunks,
)
from rag.code_graph.parser import PARSER_VERSION, CodeParser


_EXCLUDED_PARTS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build",
    ".venv", "venv", "__pycache__", ".idea", ".vscode", "target",
}


@dataclass(frozen=True)
class _PendingRelation:
    subject_local_key: str
    target_qualified_name: str
    relation_type: str
    start_line: int
    end_line: int
    confidence: float = 1.0
    metadata: dict | None = None


class CodeIndexService:
    def __init__(self, db: Session, knowledge_base_id: str, object_store=None, task_queue=None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        self.repository = CodeGraphRepository(db, knowledge_base_id)
        if object_store is None or task_queue is None:
            from app.bootstrap.container import get_provider_container
            container = get_provider_container()
            object_store = object_store or container.object_store
            task_queue = task_queue or container.task_queue
        self.object_store = object_store
        self.task_queue = task_queue
        self.parser = CodeParser()
        self._document_source_by_path: dict[str, dict] = {}

    def request_index(self, data_source_id: str, requested_by: str | None) -> IndexJob:
        source = KnowledgeBaseRepository(self.db).get_data_source(
            self.knowledge_base_id,
            data_source_id,
        )
        if source is None:
            raise ValueError("数据源不存在")
        if source.source_type not in {"local_directory", "git"}:
            raise ValueError("代码索引当前支持本地目录和 Git 数据源")
        job_repository = IndexJobRepository(self.db, self.knowledge_base_id)
        existing = job_repository.find_open(
            "code_index", option_key="data_source_id", option_value=data_source_id
        )
        if existing is not None:
            return existing
        job = job_repository.create(IndexJob(
            job_type="code_index",
            status="pending",
            requested_by=requested_by,
            total_items=1,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
            options_json={"data_source_id": data_source_id,
                          **({"prepare_archive": True} if (source.config_json or {}).get("archive_pending") else {})},
        ))
        signal_persisted_job(self.task_queue, job.id)
        return job

    def index_directory(
        self,
        root: str | Path,
        *,
        data_source_id: str | None = None,
        revision: str = "working-tree",
        merge_document_code: bool = False,
    ) -> dict:
        self._document_source_by_path.clear()
        root_path = Path(root).resolve()
        candidates = [
            path for path in sorted(root_path.rglob("*"))
            if path.is_file()
            and not path.is_symlink()
            and not any(part in _EXCLUDED_PARTS for part in path.relative_to(root_path).parts)
            and self.parser.supported(path)
            and path.stat().st_size <= CODE_MAX_FILE_SIZE_MB * 1024 * 1024
        ]
        manifest = []
        source_by_path: dict[str, bytes] = {}
        for path in candidates:
            relative = path.relative_to(root_path).as_posix()
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            manifest.append((relative, digest))
            source_by_path[relative] = data
        doc_stats: dict = {}
        from app.core import config as _config

        if merge_document_code and _config.DOCUMENT_CODE_GRAPH_ENABLED:
            doc_manifest, doc_sources, doc_stats = self._collect_document_code()
            known_paths = {path for path, _ in manifest}
            for path, digest in doc_manifest:
                if path in known_paths:
                    self._document_source_by_path.pop(path, None)
                    continue
                manifest.append((path, digest))
                source_by_path[path] = doc_sources[path]
        return self._build_snapshot_from_manifest(
            manifest,
            source_by_path,
            data_source_id=data_source_id,
            revision=revision,
            extra_stats=doc_stats if doc_stats else None,
        )

    @staticmethod
    def _combined_parser_version() -> str:
        """目录解析版本 + 文档代码提取版本；任一升级都会触发全量重建。"""
        return f"{PARSER_VERSION}+{document_code_parser_version()}"

    def _collect_document_code(
        self,
    ) -> tuple[list[tuple[str, str]], dict[str, bytes], dict]:
        """从当前知识库的 ready 文档 chunk 里提取代码块。"""
        from app.repositories.knowledge_repository import KnowledgeRepository

        repo = KnowledgeRepository(self.db, self.knowledge_base_id)
        chunk_contents: list[tuple[str, int, str]] = []
        for document in repo.list_ready_documents():
            for chunk in repo.list_chunks_for_document(document.id):
                chunk_contents.append((document.id, chunk.chunk_index, chunk.content))

        parsed_files = extract_document_code(
            chunk_contents,
            self.parser,
            max_blocks=DOCUMENT_CODE_MAX_BLOCKS,
            min_block_lines=DOCUMENT_CODE_MIN_BLOCK_LINES,
        )
        self._document_source_by_path = {
            parsed.path: dict(symbol.metadata["document_source"])
            for parsed in parsed_files
            for symbol in parsed.symbols[:1]
            if symbol.metadata.get("document_source")
        }
        manifest, source_by_path = manifest_for_document_chunks(parsed_files)
        stats = {
            "documents": len({doc_id for doc_id, _, _ in chunk_contents}),
            "code_blocks": len(parsed_files),
        }
        return manifest, source_by_path, stats

    def index_document_code(self) -> dict:
        """只从已入库文档构建代码图谱（无目录代码源时的独立入口）。

        - 若当前活动快照属于某个目录/Git 数据源：在其根目录已不可用时无法
          重新读取源码，因此这里只重建纯文档快照并接管活动位；
        - 否则直接以文档代码为唯一 manifest 构建快照。
        """
        from app.core import config as _config

        if not _config.DOCUMENT_CODE_GRAPH_ENABLED:
            return {"status": "disabled"}

        doc_manifest, doc_sources, doc_stats = self._collect_document_code()
        return self._build_snapshot_from_manifest(
            doc_manifest,
            doc_sources,
            data_source_id=None,
            revision="documents",
            extra_stats=doc_stats,
        )

    def _build_snapshot_from_manifest(
        self,
        manifest: list[tuple[str, str]],
        source_by_path: dict[str, bytes],
        *,
        data_source_id: str | None,
        revision: str,
        extra_stats: dict | None = None,
    ) -> dict:
        content_hash = hashlib.sha256(
            "\n".join(f"{path}:{digest}" for path, digest in manifest).encode("utf-8")
        ).hexdigest()
        active = self.repository.active_snapshot()
        same_source = bool(active and active.data_source_id == data_source_id)
        same_parser = bool(
            active and active.parser_version == self._combined_parser_version()
        )
        if (
            active
            and active.content_hash == content_hash
            and same_source
            and same_parser
        ):
            return {"status": "unchanged", **self.repository.stats()}

        previous_files = {}
        if active and same_source and same_parser:
            previous_files = {
                item.path: item
                for item in self.db.scalars(
                    select(CodeFile).where(CodeFile.snapshot_id == active.id)
                )
            }
        current_hashes = dict(manifest)
        reused_paths = {
            path
            for path, digest in manifest
            if path in previous_files
            and previous_files[path].content_hash == digest
        }
        parsed_paths = set(current_hashes) - reused_paths
        deleted_paths = set(previous_files) - set(current_hashes)

        snapshot = self.repository.create_snapshot(CodeSnapshot(
            knowledge_base_id=self.knowledge_base_id,
            data_source_id=data_source_id,
            revision=revision,
            content_hash=content_hash,
            parser_version=self._combined_parser_version(),
            status="building",
        ))
        try:
            parsed_by_path = self._parse_parallel({
                path: source_by_path[path] for path in parsed_paths
            })
            language_counts = Counter()
            symbol_by_local_key: dict[str, CodeSymbol] = {}
            pending_relations: list[tuple[str, _PendingRelation]] = []
            for relative, digest in manifest:
                data = source_by_path[relative]
                previous_file = previous_files.get(relative)
                if relative in reused_paths and previous_file is not None:
                    language_counts[previous_file.language] += 1
                    self._copy_unchanged_file(
                        previous_file,
                        snapshot.id,
                        symbol_by_local_key,
                        pending_relations,
                    )
                    continue

                parsed = parsed_by_path[relative]
                language_counts[parsed.language] += 1
                object_ref = self.object_store.put_bytes(
                    f"knowledge-bases/{self.knowledge_base_id}/code/{snapshot.id}/{relative}",
                    data,
                )
                code_file = self.repository.add_file(CodeFile(
                    knowledge_base_id=self.knowledge_base_id,
                    snapshot_id=snapshot.id,
                    path=relative,
                    language=parsed.language,
                    content_hash=digest,
                    file_size=len(data),
                    object_key=object_ref.key,
                    parse_status="completed" if not parsed.errors else "partial",
                    parse_errors_json=parsed.errors,
                ))
                created: list[CodeSymbol] = []
                for item in parsed.symbols:
                    symbol = CodeSymbol(
                        knowledge_base_id=self.knowledge_base_id,
                        snapshot_id=snapshot.id,
                        file_id=code_file.id,
                        stable_key=item.local_key,
                        kind=item.kind,
                        name=item.name,
                        qualified_name=item.qualified_name,
                        signature=item.signature,
                        start_line=item.start_line,
                        end_line=item.end_line,
                        source_excerpt=item.source_excerpt,
                        metadata_json={**item.metadata, "parent_local_key": item.parent_local_key},
                    )
                    created.append(symbol)
                    symbol_by_local_key[item.local_key] = symbol
                self.repository.add_symbols(created)
                for symbol in created:
                    parent_key = (symbol.metadata_json or {}).pop("parent_local_key", None)
                    if parent_key and parent_key in symbol_by_local_key:
                        symbol.parent_symbol_id = symbol_by_local_key[parent_key].id
                pending_relations.extend(
                    (
                        relative,
                        _PendingRelation(
                            subject_local_key=relation.subject_local_key,
                            target_qualified_name=relation.target_qualified_name,
                            relation_type=relation.relation_type,
                            start_line=relation.start_line,
                            end_line=relation.end_line,
                            confidence=relation.confidence,
                            metadata=dict(relation.metadata or {}),
                        ),
                    )
                    for relation in parsed.relations
                )
                self.db.flush()

            relations = self._resolve_relations(snapshot.id, symbol_by_local_key, pending_relations)
            self.repository.add_relations(relations)
            stats = {
                "files": len(manifest),
                "symbols": len(symbol_by_local_key),
                "relations": len(relations),
                "resolved_relations": sum(item.resolution_status == "resolved" for item in relations),
                "languages": dict(language_counts),
                "files_reused": len(reused_paths),
                "files_parsed": len(parsed_paths),
                "files_deleted": len(deleted_paths),
                **(extra_stats or {}),
            }
            self.repository.activate_snapshot(snapshot, stats)
            self.repository.prune_snapshots(CODE_SNAPSHOT_RETENTION)
            return {"status": "completed", "snapshot_id": snapshot.id, **stats}
        except Exception as exc:
            self.db.rollback()
            persisted = self.repository.get_snapshot(snapshot.id)
            if persisted is not None:
                self.repository.fail_snapshot(persisted, str(exc))
            raise

    def _copy_unchanged_file(
        self,
        previous_file: CodeFile,
        snapshot_id: str,
        symbol_by_local_key: dict[str, CodeSymbol],
        pending_relations: list[tuple[str, _PendingRelation]],
    ) -> None:
        """Copy durable facts while avoiding source loading and AST parsing."""
        code_file = self.repository.add_file(CodeFile(
            knowledge_base_id=self.knowledge_base_id,
            snapshot_id=snapshot_id,
            path=previous_file.path,
            language=previous_file.language,
            content_hash=previous_file.content_hash,
            file_size=previous_file.file_size,
            object_key=previous_file.object_key,
            parse_status=previous_file.parse_status,
            parse_errors_json=list(previous_file.parse_errors_json or []),
        ))
        previous_symbols = list(self.db.scalars(
            select(CodeSymbol)
            .where(CodeSymbol.file_id == previous_file.id)
            .order_by(CodeSymbol.start_line, CodeSymbol.id)
        ))
        old_to_new: dict[str, CodeSymbol] = {}
        copied_symbols = []
        for previous in previous_symbols:
            symbol = CodeSymbol(
                knowledge_base_id=self.knowledge_base_id,
                snapshot_id=snapshot_id,
                file_id=code_file.id,
                stable_key=previous.stable_key,
                kind=previous.kind,
                name=previous.name,
                qualified_name=previous.qualified_name,
                signature=previous.signature,
                start_line=previous.start_line,
                end_line=previous.end_line,
                source_excerpt=previous.source_excerpt,
                metadata_json=dict(previous.metadata_json or {}),
            )
            copied_symbols.append(symbol)
            old_to_new[previous.id] = symbol
            symbol_by_local_key[symbol.stable_key] = symbol
        self.repository.add_symbols(copied_symbols)
        for previous in previous_symbols:
            if previous.parent_symbol_id:
                old_to_new[previous.id].parent_symbol_id = old_to_new.get(
                    previous.parent_symbol_id
                ).id if previous.parent_symbol_id in old_to_new else None

        relation_rows = self.db.execute(
            select(CodeRelation, CodeSymbol.stable_key)
            .join(CodeSymbol, CodeSymbol.id == CodeRelation.subject_symbol_id)
            .where(
                CodeRelation.snapshot_id == previous_file.snapshot_id,
                CodeSymbol.file_id == previous_file.id,
            )
        ).all()
        for relation, subject_stable_key in relation_rows:
            pending_relations.append((
                previous_file.path,
                _PendingRelation(
                    subject_local_key=subject_stable_key,
                    target_qualified_name=str(
                        (relation.metadata_json or {}).get("target_reference")
                        or relation.target_qualified_name
                    ),
                    relation_type=relation.relation_type,
                    start_line=relation.evidence_start_line,
                    end_line=relation.evidence_end_line,
                    confidence=relation.confidence,
                    metadata=dict(relation.metadata_json or {}),
                ),
            ))

    def _parse_parallel(self, sources: dict[str, bytes]):
        results = {}
        with ThreadPoolExecutor(max_workers=CODE_PARSE_WORKERS, thread_name_prefix="code-parser") as pool:
            futures = {
                pool.submit(self.parser.parse, path, data.decode("utf-8", errors="replace")): path
                for path, data in sources.items()
            }
            for future in as_completed(futures):
                path = futures[future]
                parsed = future.result()
                origin = self._document_source_by_path.get(path)
                if origin:
                    for symbol in parsed.symbols:
                        symbol.metadata["document_source"] = dict(origin)
                results[path] = parsed
        return results

    def _resolve_relations(self, snapshot_id: str, symbols_by_key: dict, pending_relations):
        by_qualified = defaultdict(list)
        by_name = defaultdict(list)
        for symbol in symbols_by_key.values():
            by_qualified[symbol.qualified_name.lower()].append(symbol)
            by_name[symbol.name.lower()].append(symbol)
        result = []
        seen = set()
        for path, item in pending_relations:
            subject = symbols_by_key.get(item.subject_local_key)
            if subject is None:
                continue
            target_text = item.target_qualified_name.strip()
            candidates = by_qualified.get(target_text.lower(), [])
            if not candidates:
                candidates = by_name.get(target_text.split(".")[-1].lower(), [])
            target = candidates[0] if len(candidates) == 1 else None
            resolution = "resolved" if target else ("ambiguous" if len(candidates) > 1 else "external")
            key = (subject.id, target.id if target else target_text, item.relation_type, item.start_line)
            if key in seen:
                continue
            seen.add(key)
            metadata = dict(item.metadata or {})
            metadata.setdefault("target_reference", target_text)
            result.append(CodeRelation(
                knowledge_base_id=self.knowledge_base_id,
                snapshot_id=snapshot_id,
                subject_symbol_id=subject.id,
                target_symbol_id=target.id if target else None,
                target_qualified_name=target.qualified_name if target else target_text,
                relation_type=item.relation_type,
                confidence=item.confidence if target or item.relation_type == "IMPORTS" else min(item.confidence, 0.7),
                resolution_status=resolution,
                evidence_path=path,
                evidence_start_line=item.start_line,
                evidence_end_line=item.end_line,
                metadata_json=metadata,
            ))
        return result
