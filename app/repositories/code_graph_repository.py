"""Persistence and query operations for versioned code graphs."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from app.models.code_graph import CodeFile, CodeRelation, CodeSnapshot, CodeSymbol


class CodeGraphRepository:
    def __init__(self, db: Session, knowledge_base_id: str):
        self.db = db
        self.knowledge_base_id = knowledge_base_id

    def active_snapshot(self) -> CodeSnapshot | None:
        return self.db.scalar(
            select(CodeSnapshot)
            .where(
                CodeSnapshot.knowledge_base_id == self.knowledge_base_id,
                CodeSnapshot.status == "active",
            )
            .order_by(CodeSnapshot.activated_at.desc())
            .limit(1)
        )

    def get_snapshot(self, snapshot_id: str) -> CodeSnapshot | None:
        return self.db.scalar(select(CodeSnapshot).where(
            CodeSnapshot.id == snapshot_id,
            CodeSnapshot.knowledge_base_id == self.knowledge_base_id,
        ))

    def list_snapshots(self, limit: int = 20) -> list[CodeSnapshot]:
        return list(self.db.scalars(
            select(CodeSnapshot)
            .where(CodeSnapshot.knowledge_base_id == self.knowledge_base_id)
            .order_by(CodeSnapshot.created_at.desc())
            .limit(limit)
        ))

    def create_snapshot(self, snapshot: CodeSnapshot) -> CodeSnapshot:
        snapshot.knowledge_base_id = self.knowledge_base_id
        self.db.add(snapshot)
        self.db.commit()
        self.db.refresh(snapshot)
        return snapshot

    def add_file(self, code_file: CodeFile) -> CodeFile:
        code_file.knowledge_base_id = self.knowledge_base_id
        self.db.add(code_file)
        self.db.flush()
        return code_file

    def add_symbols(self, symbols: list[CodeSymbol]) -> None:
        for symbol in symbols:
            symbol.knowledge_base_id = self.knowledge_base_id
            self.db.add(symbol)
        self.db.flush()

    def add_relations(self, relations: list[CodeRelation]) -> None:
        for relation in relations:
            relation.knowledge_base_id = self.knowledge_base_id
            self.db.add(relation)
        self.db.flush()

    def activate_snapshot(self, snapshot: CodeSnapshot, stats: dict) -> CodeSnapshot:
        self.db.execute(
            update(CodeSnapshot)
            .where(
                CodeSnapshot.knowledge_base_id == self.knowledge_base_id,
                CodeSnapshot.status == "active",
                CodeSnapshot.id != snapshot.id,
            )
            .values(status="superseded")
        )
        snapshot.status = "active"
        snapshot.language_stats_json = stats
        snapshot.activated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(snapshot)
        return snapshot

    def fail_snapshot(self, snapshot: CodeSnapshot, error: str) -> None:
        snapshot.status = "failed"
        snapshot.error_message = error[:2000]
        self.db.commit()

    def prune_snapshots(self, retention: int) -> int:
        snapshots = self.list_snapshots(limit=1000)
        removable = [item for item in snapshots if item.status != "active"][max(0, retention - 1):]
        for snapshot in removable:
            self.db.delete(snapshot)
        if removable:
            self.db.commit()
        return len(removable)

    def search_symbols(
        self,
        query: str,
        limit: int = 20,
        snapshot_id: str | None = None,
    ) -> list[dict]:
        if snapshot_id is None:
            active = self.active_snapshot()
            if active is None:
                return []
            snapshot_id = active.id
        norm = query.strip().lower()
        if not norm:
            return []
        code_file = aliased(CodeFile)
        rows = self.db.execute(
            select(CodeSymbol, code_file.path, code_file.language)
            .join(code_file, code_file.id == CodeSymbol.file_id)
            .where(
                CodeSymbol.knowledge_base_id == self.knowledge_base_id,
                CodeSymbol.snapshot_id == snapshot_id,
                or_(
                    func.lower(CodeSymbol.name).contains(norm),
                    func.lower(CodeSymbol.qualified_name).contains(norm),
                    func.lower(func.coalesce(CodeSymbol.signature, "")).contains(norm),
                ),
            )
            .order_by(
                (func.lower(CodeSymbol.name) == norm).desc(),
                CodeSymbol.name,
                code_file.path,
            )
            .limit(limit)
        ).all()
        return [self._symbol_dict(symbol, path, language) for symbol, path, language in rows]

    def list_symbols(
        self,
        snapshot_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        if snapshot_id is None:
            active = self.active_snapshot()
            if active is None:
                return []
            snapshot_id = active.id
        statement = (
            select(CodeSymbol, CodeFile.path, CodeFile.language)
            .join(CodeFile, CodeFile.id == CodeSymbol.file_id)
            .where(
                CodeSymbol.knowledge_base_id == self.knowledge_base_id,
                CodeSymbol.snapshot_id == snapshot_id,
            )
            .order_by(CodeFile.path, CodeSymbol.start_line, CodeSymbol.id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return [
            self._symbol_dict(symbol, path, language)
            for symbol, path, language in self.db.execute(statement).all()
        ]

    def get_symbols(self, symbol_ids: list[str]) -> list[dict]:
        if not symbol_ids:
            return []
        rows = self.db.execute(
            select(CodeSymbol, CodeFile.path, CodeFile.language)
            .join(CodeFile, CodeFile.id == CodeSymbol.file_id)
            .where(
                CodeSymbol.knowledge_base_id == self.knowledge_base_id,
                CodeSymbol.id.in_(symbol_ids),
            )
        ).all()
        by_id = {
            symbol.id: self._symbol_dict(symbol, path, language)
            for symbol, path, language in rows
        }
        return [by_id[symbol_id] for symbol_id in symbol_ids if symbol_id in by_id]

    def get_symbol(self, symbol_id: str) -> dict | None:
        row = self.db.execute(
            select(CodeSymbol, CodeFile.path, CodeFile.language)
            .join(CodeFile, CodeFile.id == CodeSymbol.file_id)
            .where(
                CodeSymbol.id == symbol_id,
                CodeSymbol.knowledge_base_id == self.knowledge_base_id,
            )
        ).first()
        return self._symbol_dict(*row) if row else None

    def symbol_relations(self, symbol_ids: list[str], limit: int = 100) -> list[dict]:
        if not symbol_ids:
            return []
        subject = aliased(CodeSymbol)
        target = aliased(CodeSymbol)
        rows = self.db.execute(
            select(CodeRelation, subject, target)
            .join(subject, subject.id == CodeRelation.subject_symbol_id)
            .outerjoin(target, target.id == CodeRelation.target_symbol_id)
            .where(
                CodeRelation.knowledge_base_id == self.knowledge_base_id,
                or_(
                    CodeRelation.subject_symbol_id.in_(symbol_ids),
                    CodeRelation.target_symbol_id.in_(symbol_ids),
                ),
            )
            .order_by(CodeRelation.relation_type, CodeRelation.id)
            .limit(limit)
        ).all()
        return [{
            "id": relation.id,
            "source": relation.subject_symbol_id,
            "source_name": source.qualified_name,
            "target": relation.target_symbol_id,
            "target_name": target.qualified_name if target else relation.target_qualified_name,
            "relation_type": relation.relation_type,
            "confidence": relation.confidence,
            "resolution_status": relation.resolution_status,
            "path": relation.evidence_path,
            "start_line": relation.evidence_start_line,
            "end_line": relation.evidence_end_line,
        } for relation, source, target in rows]

    def relation_context(
        self,
        snapshot_id: str,
        per_symbol_limit: int = 12,
    ) -> dict[str, list[str]]:
        """Return compact incoming/outgoing relation facts for summary generation."""
        subject = aliased(CodeSymbol)
        target = aliased(CodeSymbol)
        rows = self.db.execute(
            select(CodeRelation, subject, target)
            .join(subject, subject.id == CodeRelation.subject_symbol_id)
            .outerjoin(target, target.id == CodeRelation.target_symbol_id)
            .where(
                CodeRelation.knowledge_base_id == self.knowledge_base_id,
                CodeRelation.snapshot_id == snapshot_id,
            )
            .order_by(
                CodeRelation.evidence_path,
                CodeRelation.evidence_start_line,
                CodeRelation.id,
            )
        ).all()
        context: dict[str, list[str]] = {}

        def append(symbol_id: str | None, fact: str) -> None:
            if not symbol_id:
                return
            facts = context.setdefault(symbol_id, [])
            if fact not in facts and len(facts) < per_symbol_limit:
                facts.append(fact)

        for relation, source_symbol, target_symbol in rows:
            target_name = (
                target_symbol.qualified_name
                if target_symbol is not None
                else relation.target_qualified_name
            )
            append(
                source_symbol.id,
                f"{relation.relation_type} -> {target_name}",
            )
            if target_symbol is not None:
                append(
                    target_symbol.id,
                    f"{relation.relation_type} <- {source_symbol.qualified_name}",
                )
        return context

    def update_symbol_metadata(self, symbol_id: str, metadata: dict) -> None:
        self.db.execute(
            update(CodeSymbol)
            .where(
                CodeSymbol.id == symbol_id,
                CodeSymbol.knowledge_base_id == self.knowledge_base_id,
            )
            .values(metadata_json=metadata)
        )

    def stats(self) -> dict:
        snapshot = self.active_snapshot()
        if snapshot is None:
            return {"active_snapshot_id": None, "files": 0, "symbols": 0, "relations": 0, "resolved_relations": 0}
        counts = {
            "files": self.db.scalar(select(func.count(CodeFile.id)).where(CodeFile.snapshot_id == snapshot.id)) or 0,
            "symbols": self.db.scalar(select(func.count(CodeSymbol.id)).where(CodeSymbol.snapshot_id == snapshot.id)) or 0,
            "relations": self.db.scalar(select(func.count(CodeRelation.id)).where(CodeRelation.snapshot_id == snapshot.id)) or 0,
            "resolved_relations": self.db.scalar(select(func.count(CodeRelation.id)).where(
                CodeRelation.snapshot_id == snapshot.id,
                CodeRelation.resolution_status == "resolved",
            )) or 0,
        }
        return {"active_snapshot_id": snapshot.id, "revision": snapshot.revision, **counts}

    @staticmethod
    def _symbol_dict(symbol: CodeSymbol, path: str, language: str) -> dict:
        metadata = symbol.metadata_json or {}
        return {
            "id": symbol.id,
            "stable_key": symbol.stable_key,
            "snapshot_id": symbol.snapshot_id,
            "file_id": symbol.file_id,
            "path": path,
            "language": language,
            "kind": symbol.kind,
            "name": symbol.name,
            "qualified_name": symbol.qualified_name,
            "signature": symbol.signature,
            "start_line": symbol.start_line,
            "end_line": symbol.end_line,
            "source_excerpt": symbol.source_excerpt,
            "functional_summary": metadata.get("functional_summary"),
            "metadata": metadata,
        }
