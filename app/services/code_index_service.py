"""Build versioned code symbol/call/dependency graphs from source snapshots."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import CODE_MAX_FILE_SIZE_MB, CODE_PARSE_WORKERS, CODE_SNAPSHOT_RETENTION
from app.core.config import INDEX_JOB_MAX_ATTEMPTS
from app.models.code_graph import CodeFile, CodeRelation, CodeSnapshot, CodeSymbol
from app.models.index_job import IndexJob
from app.repositories.code_graph_repository import CodeGraphRepository
from app.repositories.index_job_repository import IndexJobRepository
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag.code_graph.parser import PARSER_VERSION, CodeParser


_EXCLUDED_PARTS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build",
    ".venv", "venv", "__pycache__", ".idea", ".vscode", "target",
}


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
            options_json={"data_source_id": data_source_id},
        ))
        self.task_queue.enqueue(job.id)
        return job

    def index_directory(
        self,
        root: str | Path,
        *,
        data_source_id: str | None = None,
        revision: str = "working-tree",
    ) -> dict:
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
        content_hash = hashlib.sha256(
            "\n".join(f"{path}:{digest}" for path, digest in manifest).encode("utf-8")
        ).hexdigest()
        active = self.repository.active_snapshot()
        if active and active.content_hash == content_hash and active.data_source_id == data_source_id:
            return {"status": "unchanged", **self.repository.stats()}

        snapshot = self.repository.create_snapshot(CodeSnapshot(
            knowledge_base_id=self.knowledge_base_id,
            data_source_id=data_source_id,
            revision=revision,
            content_hash=content_hash,
            parser_version=PARSER_VERSION,
            status="building",
        ))
        try:
            parsed_by_path = self._parse_parallel(source_by_path)
            language_counts = Counter()
            symbol_by_local_key: dict[str, CodeSymbol] = {}
            pending_relations = []
            for relative, digest in manifest:
                data = source_by_path[relative]
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
                pending_relations.extend((relative, relation) for relation in parsed.relations)
                self.db.flush()

            relations = self._resolve_relations(snapshot.id, symbol_by_local_key, pending_relations)
            self.repository.add_relations(relations)
            stats = {
                "files": len(manifest),
                "symbols": len(symbol_by_local_key),
                "relations": len(relations),
                "resolved_relations": sum(item.resolution_status == "resolved" for item in relations),
                "languages": dict(language_counts),
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

    def _parse_parallel(self, sources: dict[str, bytes]):
        results = {}
        with ThreadPoolExecutor(max_workers=CODE_PARSE_WORKERS, thread_name_prefix="code-parser") as pool:
            futures = {
                pool.submit(self.parser.parse, path, data.decode("utf-8", errors="replace")): path
                for path, data in sources.items()
            }
            for future in as_completed(futures):
                path = futures[future]
                results[path] = future.result()
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
                metadata_json=item.metadata,
            ))
        return result
