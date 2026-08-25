"""Versioned structural and functional semantic indexes for code symbols."""

from __future__ import annotations

import hashlib
import json
import re

from langchain_core.documents import Document
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import (
    CODE_FUNCTION_SUMMARY_BATCH_SIZE,
    CODE_FUNCTION_SUMMARY_LLM_ENABLED,
    CODE_FUNCTION_SUMMARY_MAX_CHARS,
    CODE_FUNCTION_SUMMARY_MAX_SOURCE_CHARS,
    CODE_FUNCTION_SUMMARY_RELATION_LIMIT,
    CODE_FUNCTION_VECTOR_ENABLED,
    CODE_VECTOR_ENABLED,
    CODE_VECTOR_MAX_CHARS,
    INGEST_BATCH_SIZE,
    INDEX_JOB_MAX_ATTEMPTS,
)
from app.models.code_graph import CodeVectorIndexState
from app.models.index_job import IndexJob
from app.repositories.code_graph_repository import CodeGraphRepository
from app.repositories.index_job_repository import IndexJobRepository
from rag.providers.interfaces import VectorIndexRef


_SUMMARY_VERSION = "code-function-summary-v1"
_STRUCTURE_VECTOR_KIND = "structure"
_FUNCTION_VECTOR_KIND = "function_summary"


class CodeVectorService:
    def __init__(self, db: Session, knowledge_base_id: str, container=None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        self.repository = CodeGraphRepository(db, knowledge_base_id)
        if container is None:
            from app.bootstrap.container import get_provider_container

            container = get_provider_container()
        self.container = container

    def state(self) -> CodeVectorIndexState | None:
        return self.db.scalar(select(CodeVectorIndexState).where(
            CodeVectorIndexState.knowledge_base_id == self.knowledge_base_id
        ))

    def request_rebuild(self, requested_by: str | None) -> IndexJob:
        repository = IndexJobRepository(self.db, self.knowledge_base_id)
        existing = repository.find_open("code_vector_rebuild")
        if existing is not None:
            return existing
        job = repository.create(IndexJob(
            job_type="code_vector_rebuild",
            status="pending",
            requested_by=requested_by,
            total_items=1,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
        ))
        self.container.task_queue.enqueue(job.id)
        return job

    def rebuild(self) -> dict:
        snapshot = self.repository.active_snapshot()
        state = self.state()
        old_index_name = state.index_name if state else None
        if state is None:
            state = CodeVectorIndexState(knowledge_base_id=self.knowledge_base_id)
            self.db.add(state)

        if snapshot is None:
            state.snapshot_id = None
            state.index_name = None
            state.embedding_fingerprint = ""
            state.symbol_fingerprint = ""
            state.symbol_count = 0
            state.status = "empty"
            self.db.commit()
            self._delete_index(old_index_name)
            return {"status": "empty", "symbol_count": 0, "index_name": None}

        symbols = self.repository.list_symbols(snapshot.id)
        summary_stats = {
            "summaries_generated": 0,
            "summaries_reused": 0,
            "summaries_fallback": 0,
        }
        if CODE_FUNCTION_VECTOR_ENABLED and symbols:
            summary_stats = self._ensure_functional_summaries(symbols, snapshot.id)

        embedding_identity = self.container.embedding.identity().as_dict()
        embedding_fingerprint = json.dumps(
            embedding_identity, sort_keys=True, ensure_ascii=False
        )
        rendered = self._render_vector_entries(symbols)
        content_hashes = {
            key: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for key, content in rendered.items()
        }
        symbol_fingerprint = hashlib.sha256("\n".join(
            f"{kind}:{stable_key}:{content_hashes[(kind, stable_key)]}"
            for kind, stable_key in sorted(content_hashes)
        ).encode("utf-8")).hexdigest()
        if (
            state.status == "ready"
            and state.snapshot_id == snapshot.id
            and state.embedding_fingerprint == embedding_fingerprint
            and state.symbol_fingerprint == symbol_fingerprint
        ):
            return {
                "status": "unchanged",
                "symbol_count": state.symbol_count,
                "vector_count": len(rendered),
                "index_name": state.index_name,
                "snapshot_id": state.snapshot_id,
                **summary_stats,
            }

        can_increment = bool(
            state.status == "ready"
            and state.snapshot_id
            and state.index_name
            and state.index_name.startswith("code_symbols_v3_")
            and state.embedding_fingerprint == embedding_fingerprint
            and self.repository.get_snapshot(state.snapshot_id) is not None
        )
        previous_symbols = (
            self.repository.list_symbols(state.snapshot_id)
            if can_increment and state.snapshot_id
            else []
        )
        previous_rendered = self._render_vector_entries(previous_symbols)
        previous_hashes = {
            key: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for key, content in previous_rendered.items()
        }

        state.status = "building"
        self.db.commit()
        if not symbols:
            state.snapshot_id = snapshot.id
            state.index_name = None
            state.embedding_fingerprint = embedding_fingerprint
            state.symbol_fingerprint = symbol_fingerprint
            state.symbol_count = 0
            state.status = "ready"
            self.db.commit()
            self._delete_index(old_index_name)
            return {
                "status": "ready",
                "symbol_count": 0,
                "vector_count": 0,
                "index_name": None,
                "snapshot_id": snapshot.id,
                **summary_stats,
            }

        compact_kb = self.knowledge_base_id.replace("-", "")[:12]
        embedding_hash = hashlib.sha256(
            embedding_fingerprint.encode("utf-8")
        ).hexdigest()
        index_name = (
            state.index_name
            if can_increment
            else f"code_symbols_v3_{compact_kb}_{embedding_hash[:8]}_{symbol_fingerprint[:8]}"
        )
        index_ref = VectorIndexRef(
            name=index_name,
            knowledge_base_id=self.knowledge_base_id,
            index_version=symbol_fingerprint[:12],
            embedding_profile_id=embedding_hash[:12],
        )
        if not can_increment:
            self._delete_index(index_name)
        store = self.container.vector_store.open(
            self.container.embedding.get_model(), index_ref
        )

        current_keys = set(content_hashes)
        previous_keys = set(previous_hashes)
        deleted_keys = previous_keys - current_keys if can_increment else set()
        changed_keys = {
            key
            for key in current_keys
            if not can_increment or previous_hashes.get(key) != content_hashes[key]
        }
        unchanged_keys = current_keys - changed_keys
        symbols_by_key = {symbol["stable_key"]: symbol for symbol in symbols}
        ordered_changed = sorted(changed_keys)
        documents = [
            self._vector_document(
                symbols_by_key[stable_key],
                rendered[(kind, stable_key)],
                snapshot.id,
                kind,
            )
            for kind, stable_key in ordered_changed
        ]
        ids = [
            self._vector_id(stable_key, kind)
            for kind, stable_key in ordered_changed
        ]
        try:
            stale_ids = [
                self._vector_id(stable_key, kind)
                for kind, stable_key in sorted(deleted_keys | changed_keys)
            ]
            if can_increment and stale_ids:
                self.container.vector_store.delete_vectors(index_ref, ids=stale_ids)
            for start in range(0, len(documents), INGEST_BATCH_SIZE):
                store.add_documents(
                    documents[start:start + INGEST_BATCH_SIZE],
                    ids=ids[start:start + INGEST_BATCH_SIZE],
                )
            state.snapshot_id = snapshot.id
            state.index_name = index_name
            state.embedding_fingerprint = embedding_fingerprint
            state.symbol_fingerprint = symbol_fingerprint
            state.symbol_count = len(symbols)
            state.status = "ready"
            self.db.commit()
        except Exception:
            state.status = "failed"
            self.db.commit()
            raise
        if old_index_name and old_index_name != index_name:
            self._delete_index(old_index_name)

        changed_symbols = {stable_key for _, stable_key in changed_keys}
        deleted_symbols = {stable_key for _, stable_key in deleted_keys}
        unchanged_symbols = {
            stable_key for _, stable_key in unchanged_keys
        } - changed_symbols
        return {
            "status": "ready",
            "symbol_count": len(symbols),
            "vector_count": len(rendered),
            "index_name": index_name,
            "snapshot_id": snapshot.id,
            "symbols_added_or_updated": len(changed_symbols),
            "symbols_deleted": len(deleted_symbols),
            "symbols_reused": len(unchanged_symbols),
            "vectors_added_or_updated": len(changed_keys),
            "vectors_deleted": len(deleted_keys),
            "vectors_reused": len(unchanged_keys),
            **summary_stats,
        }

    def semantic_search(
        self,
        query: str,
        limit: int = 10,
        query_embedding: list[float] | None = None,
    ) -> list[dict]:
        state = self.state()
        active = self.repository.active_snapshot()
        if (
            state is None
            or state.status != "ready"
            or not state.index_name
            or active is None
            or state.snapshot_id != active.id
        ):
            return []
        store = self.container.vector_store.open(
            self.container.embedding.get_model(),
            VectorIndexRef(
                name=state.index_name,
                knowledge_base_id=self.knowledge_base_id,
            ),
        )
        candidate_k = max(limit, limit * (2 if CODE_FUNCTION_VECTOR_ENABLED else 1))
        try:
            if query_embedding is not None:
                rows = store.similarity_search_by_vector_with_relevance_scores(
                    query_embedding, k=candidate_k
                )
            else:
                rows = store.similarity_search_with_relevance_scores(
                    query, k=candidate_k
                )
            return self._hydrate_semantic_rows(rows, active.id, limit)
        except (AttributeError, NotImplementedError):
            documents = (
                store.similarity_search_by_vector(query_embedding, k=candidate_k)
                if query_embedding is not None
                else store.similarity_search(query, k=candidate_k)
            )
            return self._hydrate_semantic_rows([
                (document, 1.0 / (index + 1))
                for index, document in enumerate(documents)
            ], active.id, limit)

    def hybrid_search(
        self,
        query: str,
        limit: int,
        query_embedding: list[float] | None = None,
    ) -> list[dict]:
        lexical = self.repository.search_symbols(query, limit)
        semantic = []
        if CODE_VECTOR_ENABLED:
            try:
                semantic = self.semantic_search(
                    query,
                    limit,
                    query_embedding=query_embedding,
                )
            except Exception:
                semantic = []

        fused: dict[str, dict] = {}
        for method, rows in (("lexical", lexical), ("vector", semantic)):
            for rank, item in enumerate(rows, start=1):
                symbol_id = item.get("id") or item.get("symbol_id")
                if not symbol_id:
                    continue
                entry = fused.setdefault(symbol_id, {
                    "score": 0.0,
                    "methods": [],
                    "vector_match_types": [],
                })
                entry["score"] += 1.0 / (60 + rank)
                if method not in entry["methods"]:
                    entry["methods"].append(method)
                for match_type in item.get("vector_match_types", []):
                    if match_type not in entry["vector_match_types"]:
                        entry["vector_match_types"].append(match_type)

        lexical_by_id = {item["id"]: item for item in lexical}
        missing_ids = [
            symbol_id for symbol_id in fused if symbol_id not in lexical_by_id
        ]
        hydrated = {
            item["id"]: item for item in self.repository.get_symbols(missing_ids)
        }
        ranked_ids = sorted(
            fused,
            key=lambda symbol_id: (-float(fused[symbol_id]["score"]), symbol_id),
        )[:limit]
        results = []
        for symbol_id in ranked_ids:
            symbol = lexical_by_id.get(symbol_id) or hydrated.get(symbol_id)
            if symbol is None:
                continue
            results.append({
                **symbol,
                "retrieval_score": fused[symbol_id]["score"],
                "retrieval_methods": fused[symbol_id]["methods"],
                "vector_match_types": fused[symbol_id]["vector_match_types"],
            })
        return results

    def _ensure_functional_summaries(
        self,
        symbols: list[dict],
        snapshot_id: str,
    ) -> dict:
        relation_context = self.repository.relation_context(
            snapshot_id,
            CODE_FUNCTION_SUMMARY_RELATION_LIMIT,
        )
        llm = self._summary_llm()
        model_fingerprint = self._summary_model_fingerprint(llm)
        pending = []
        reused = 0
        for symbol in symbols:
            relations = relation_context.get(symbol["id"], [])
            source_hash = self._summary_source_hash(
                symbol, relations, model_fingerprint
            )
            metadata = dict(symbol.get("metadata") or {})
            summary = str(metadata.get("functional_summary") or "").strip()
            reusable = (
                summary
                and metadata.get("functional_summary_source_hash") == source_hash
                and (
                    metadata.get("functional_summary_origin") == "llm"
                    or llm is None
                )
            )
            if reusable:
                symbol["functional_summary"] = summary
                reused += 1
                continue
            pending.append({
                "symbol": symbol,
                "relations": relations,
                "source_hash": source_hash,
            })

        generated = 0
        fallback = 0
        batch_size = max(1, CODE_FUNCTION_SUMMARY_BATCH_SIZE)
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            summaries = {}
            if llm is not None:
                try:
                    summaries = self._generate_summary_batch(llm, batch)
                except Exception:
                    llm = None
            for item in batch:
                symbol = item["symbol"]
                summary = summaries.get(symbol["stable_key"])
                origin = "llm" if summary else "fallback"
                if not summary:
                    summary = self._fallback_summary(symbol, item["relations"])
                    fallback += 1
                else:
                    generated += 1
                summary = summary.strip()[:CODE_FUNCTION_SUMMARY_MAX_CHARS]
                metadata = dict(symbol.get("metadata") or {})
                metadata.update({
                    "functional_summary": summary,
                    "functional_summary_source_hash": item["source_hash"],
                    "functional_summary_model": model_fingerprint,
                    "functional_summary_version": _SUMMARY_VERSION,
                    "functional_summary_origin": origin,
                })
                self.repository.update_symbol_metadata(symbol["id"], metadata)
                symbol["metadata"] = metadata
                symbol["functional_summary"] = summary
        if pending:
            self.db.commit()
        return {
            "summaries_generated": generated,
            "summaries_reused": reused,
            "summaries_fallback": fallback,
        }

    def _summary_llm(self):
        if not CODE_FUNCTION_SUMMARY_LLM_ENABLED:
            return None
        return (
            getattr(self.container, "extraction_llm", None)
            or getattr(self.container, "llm", None)
        )

    @staticmethod
    def _summary_model_fingerprint(llm) -> str:
        if llm is None:
            return "deterministic"
        identity = getattr(llm, "identity", None)
        if callable(identity):
            try:
                return json.dumps(
                    identity().as_dict(), sort_keys=True, ensure_ascii=False
                )
            except Exception:
                pass
        return type(llm).__name__

    @staticmethod
    def _summary_source_hash(
        symbol: dict,
        relations: list[str],
        model_fingerprint: str,
    ) -> str:
        payload = {
            "version": _SUMMARY_VERSION,
            "model": model_fingerprint,
            "path": symbol["path"],
            "kind": symbol["kind"],
            "qualified_name": symbol["qualified_name"],
            "signature": symbol.get("signature") or "",
            "source": (symbol.get("source_excerpt") or "")[
                :CODE_FUNCTION_SUMMARY_MAX_SOURCE_CHARS
            ],
            "relations": relations,
        }
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")).hexdigest()

    def _generate_summary_batch(self, llm, batch: list[dict]) -> dict[str, str]:
        items = [{
            "stable_key": item["symbol"]["stable_key"],
            "path": item["symbol"]["path"],
            "kind": item["symbol"]["kind"],
            "qualified_name": item["symbol"]["qualified_name"],
            "signature": item["symbol"].get("signature") or "",
            "relations": item["relations"],
            "source_excerpt": (item["symbol"].get("source_excerpt") or "")[
                :CODE_FUNCTION_SUMMARY_MAX_SOURCE_CHARS
            ],
        } for item in batch]
        prompt = (
            "你是企业代码索引的功能摘要器。根据符号名称、路径、签名、源码和调用关系，"
            "为每个符号生成1到3句简洁中文摘要。优先说明业务用途、触发条件、输入输出、"
            "副作用和调用的关键能力；无法从代码确认的内容不要猜测。仅输出JSON数组，"
            "每项格式为{\"stable_key\":\"...\",\"summary\":\"...\"}，不要Markdown。\n"
            f"符号：{json.dumps(items, ensure_ascii=False)}"
        )
        raw = llm.generate(prompt).strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S | re.I)
        if fenced:
            raw = fenced.group(1).strip()
        payload = json.loads(raw)
        if isinstance(payload, dict):
            payload = payload.get("summaries", [])
        allowed = {item["symbol"]["stable_key"] for item in batch}
        result = {}
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            stable_key = str(item.get("stable_key") or "")
            summary = str(item.get("summary") or "").strip()
            if stable_key in allowed and summary:
                result[stable_key] = summary[:CODE_FUNCTION_SUMMARY_MAX_CHARS]
        return result

    @staticmethod
    def _fallback_summary(symbol: dict, relations: list[str]) -> str:
        kind_labels = {
            "FILE": "代码文件",
            "CLASS": "类",
            "FUNCTION": "函数",
            "ASYNC_FUNCTION": "异步函数",
            "METHOD": "方法",
            "ASYNC_METHOD": "异步方法",
        }
        kind = kind_labels.get(symbol["kind"], symbol["kind"])
        parts = [f"{kind} {symbol['qualified_name']} 位于 {symbol['path']}。"]
        if symbol.get("signature"):
            parts.append(f"签名为 {symbol['signature']}。")
        if relations:
            parts.append(f"相关调用与依赖：{'；'.join(relations)}。")
        return "".join(parts)

    def _render_vector_entries(self, symbols: list[dict]) -> dict[tuple[str, str], str]:
        rendered = {}
        for symbol in symbols:
            stable_key = symbol["stable_key"]
            rendered[(_STRUCTURE_VECTOR_KIND, stable_key)] = self._render_symbol(
                symbol
            )
            if CODE_FUNCTION_VECTOR_ENABLED:
                summary = str(symbol.get("functional_summary") or "").strip()
                if summary:
                    rendered[(_FUNCTION_VECTOR_KIND, stable_key)] = (
                        self._render_functional_symbol(symbol, summary)
                    )
        return rendered

    @staticmethod
    def _render_symbol(symbol: dict) -> str:
        return "\n".join(filter(None, [
            f"语言：{symbol['language']}",
            f"路径：{symbol['path']}",
            f"类型：{symbol['kind']}",
            f"符号：{symbol['qualified_name']}",
            symbol.get("signature"),
            symbol.get("source_excerpt"),
        ]))[:CODE_VECTOR_MAX_CHARS]

    @staticmethod
    def _render_functional_symbol(symbol: dict, summary: str) -> str:
        return "\n".join(filter(None, [
            f"功能摘要：{summary}",
            f"符号：{symbol['qualified_name']}",
            f"路径：{symbol['path']}",
            f"类型：{symbol['kind']}",
            symbol.get("signature"),
        ]))[:CODE_VECTOR_MAX_CHARS]

    @staticmethod
    def _vector_id(stable_key: str, vector_kind: str) -> str:
        digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()
        return f"code-symbol:{vector_kind}:{digest}"

    def _vector_document(
        self,
        symbol: dict,
        page_content: str,
        snapshot_id: str,
        vector_kind: str,
    ) -> Document:
        return Document(
            page_content=page_content,
            metadata={
                "stable_key": symbol["stable_key"],
                "snapshot_id": snapshot_id,
                "knowledge_base_id": self.knowledge_base_id,
                "path": symbol["path"],
                "language": symbol["language"],
                "kind": symbol["kind"],
                "qualified_name": symbol["qualified_name"],
                "vector_kind": vector_kind,
            },
        )

    def _hydrate_semantic_rows(
        self,
        rows,
        snapshot_id: str,
        limit: int,
    ) -> list[dict]:
        current = {
            symbol["stable_key"]: symbol
            for symbol in self.repository.list_symbols(snapshot_id)
        }
        grouped: dict[str, dict] = {}
        for rank, (document, score) in enumerate(rows, start=1):
            stable_key = document.metadata.get("stable_key")
            symbol = current.get(stable_key)
            if symbol is None:
                continue
            entry = grouped.setdefault(stable_key, {
                "symbol": symbol,
                "rrf_score": 0.0,
                "match_score": float(score),
                "vector_match_types": [],
            })
            entry["rrf_score"] += 1.0 / (60 + rank)
            entry["match_score"] = max(entry["match_score"], float(score))
            vector_kind = document.metadata.get(
                "vector_kind", _STRUCTURE_VECTOR_KIND
            )
            if vector_kind not in entry["vector_match_types"]:
                entry["vector_match_types"].append(vector_kind)
        ranked = sorted(
            grouped.values(),
            key=lambda item: (-item["rrf_score"], -item["match_score"]),
        )[:limit]
        return [{
            **item["symbol"],
            "match_score": item["match_score"],
            "semantic_rrf_score": item["rrf_score"],
            "vector_match_types": item["vector_match_types"],
        } for item in ranked]

    def _delete_index(self, index_name: str | None) -> None:
        if not index_name:
            return
        try:
            self.container.vector_store.delete_index(VectorIndexRef(
                name=index_name,
                knowledge_base_id=self.knowledge_base_id,
            ))
        except Exception:
            pass
