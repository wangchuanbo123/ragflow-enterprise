"""Versioned structural and functional semantic indexes for code symbols."""

from __future__ import annotations

import hashlib
import json
import re
from time import perf_counter

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
from app.repositories.index_job_repository import IndexJobRepository, signal_persisted_job
from rag.providers.interfaces import VectorIndexRef
from app.agents.contracts import AgentCancelledError
from rag.code_graph.agent_summary import AgentCodeSummarizer


_SUMMARY_VERSION = "code-function-summary-v2-agent"
_STRUCTURE_VECTOR_KIND = "structure"
_FUNCTION_VECTOR_KIND = "function_summary"


class CodeVectorService:
    def __init__(self, db: Session, knowledge_base_id: str, container=None, *,
                 summary_llm=None, agent_backend=None, summary_agent_enabled=None,
                 agent_context=None, cancelled=None, on_event=None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        self.repository = CodeGraphRepository(db, knowledge_base_id)
        if container is None:
            from app.bootstrap.container import get_provider_container

            container = get_provider_container()
        self.container = container
        self._injected_summary_llm = summary_llm
        self._agent_backend = agent_backend
        self._summary_agent_enabled = CODE_FUNCTION_SUMMARY_LLM_ENABLED if summary_agent_enabled is None else summary_agent_enabled
        self._agent_context = agent_context or {}
        self._cancelled = cancelled
        self._on_event = on_event
        self._summary_usage = []

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
        signal_persisted_job(self.container.task_queue, job.id)
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
        symbols_by_key = {symbol["stable_key"]: symbol for symbol in symbols}
        content_hashes = {
            key: self._vector_content_hash(content, key[0], symbols_by_key[key[1]])
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
            and (not symbols or (state.index_name or "").startswith("code_symbols_v4_"))
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
            and state.index_name.startswith("code_symbols_v4_")
            and state.embedding_fingerprint == embedding_fingerprint
            and self.repository.get_snapshot(state.snapshot_id) is not None
            # Same-snapshot summary regeneration has already updated the ORM
            # metadata, so it cannot reconstruct the previous rendered vectors.
            # Build a new collection instead of comparing current data to itself.
            and (state.snapshot_id != snapshot.id or state.symbol_fingerprint == symbol_fingerprint)
        )
        previous_symbols = (
            self.repository.list_symbols(state.snapshot_id)
            if can_increment and state.snapshot_id
            else []
        )
        previous_rendered = self._render_vector_entries(previous_symbols)
        previous_by_key = {symbol["stable_key"]: symbol for symbol in previous_symbols}
        previous_hashes = {
            key: self._vector_content_hash(content, key[0], previous_by_key[key[1]])
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
            else f"code_symbols_v4_{compact_kb}_{embedding_hash[:8]}_{symbol_fingerprint[:8]}"
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
        allowed_document_ids: set[str] | None = None,
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
        search_options = (
            {"filter": {"document_id": {"$in": sorted(allowed_document_ids)}}}
            if allowed_document_ids is not None else {}
        )
        if allowed_document_ids is not None and not allowed_document_ids:
            return []
        try:
            if query_embedding is not None:
                rows = store.similarity_search_by_vector_with_relevance_scores(
                    query_embedding, k=candidate_k, **search_options
                )
            else:
                rows = store.similarity_search_with_relevance_scores(
                    query, k=candidate_k, **search_options
                )
            return self._hydrate_semantic_rows(rows, active.id, limit)
        except (AttributeError, NotImplementedError):
            documents = (
                store.similarity_search_by_vector(query_embedding, k=candidate_k, **search_options)
                if query_embedding is not None
                else store.similarity_search(query, k=candidate_k, **search_options)
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
        allowed_document_ids: set[str] | None = None,
        diagnostics: list[dict] | None = None,
    ) -> list[dict]:
        lexical = self.repository.search_symbols(
            query, limit, allowed_document_ids=allowed_document_ids,
        )
        semantic = []
        if CODE_VECTOR_ENABLED:
            started = perf_counter()
            try:
                semantic = self.semantic_search(
                    query,
                    limit,
                    query_embedding=query_embedding,
                    **({"allowed_document_ids": allowed_document_ids}
                       if allowed_document_ids is not None else {}),
                )
            except Exception as exc:
                semantic = []
                if diagnostics is not None:
                    diagnostics.append({"channel": "code", "status": "degraded",
                        "code": "vector_" + type(exc).__name__, "count": len(lexical),
                        "elapsed_ms": round((perf_counter() - started) * 1000, 2)})

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
        llm = self._injected_summary_llm
        summarizer = None
        diagnostic = "agent_disabled"
        self._summary_usage = []
        if llm is not None:
            diagnostic = None
            mode = "injected_llm"
            model_fingerprint = self._summary_model_fingerprint(llm)
        elif self._summary_agent_enabled:
            mode = "agent"
            try:
                if self._agent_backend is None:
                    from app.agents.factory import get_agent_backend

                    self._agent_backend = get_agent_backend(self._agent_context.get("profile_name"))
                summarizer = AgentCodeSummarizer(
                    self._agent_backend, self.knowledge_base_id, agent_context=self._agent_context,
                    cancelled=self._cancelled, on_event=self._record_summary_event,
                    max_chars=CODE_FUNCTION_SUMMARY_MAX_CHARS,
                )
                model_fingerprint = json.dumps(summarizer.identity(), sort_keys=True, ensure_ascii=False)
                diagnostic = None
            except Exception as exc:
                diagnostic = getattr(exc, "code", "AGENT_UNAVAILABLE")
                model_fingerprint = f"{_SUMMARY_VERSION}:agent_unavailable:{type(exc).__name__}"
        else:
            mode = "deterministic"
            model_fingerprint = f"{_SUMMARY_VERSION}:deterministic"
        pending = []
        reused = 0
        methods = set()
        scope_skipped = 0
        scope = self._agent_context.get("scope_document_ids")
        scope = set(scope) if scope is not None else None
        for symbol in symbols:
            relations = relation_context.get(symbol["id"], [])
            metadata = dict(symbol.get("metadata") or {})
            source_document_id = (metadata.get("document_source") or {}).get("document_id")
            eligible = ((scope is None or source_document_id in scope) if source_document_id
                        else self._agent_context.get("allow_repository_symbols", True))
            item_fingerprint = model_fingerprint if eligible else f"{_SUMMARY_VERSION}:outside_agent_scope"
            source_hash = self._summary_source_hash(
                symbol, relations, item_fingerprint
            )
            summary = str(metadata.get("functional_summary") or "").strip()
            if not eligible:
                scope_skipped += 1
                if summary:
                    symbol["functional_summary"] = summary
                    reused += 1
                    methods.add(metadata.get("summary_method", "existing"))
                    continue
            reusable = (
                summary
                and metadata.get("functional_summary_source_hash") == source_hash
                and (
                    metadata.get("functional_summary_origin") == ("llm" if mode == "injected_llm" else "agent" if mode == "agent" else "fallback")
                )
            )
            if reusable:
                symbol["functional_summary"] = summary
                reused += 1
                methods.add(metadata.get("summary_method", "deterministic"))
                continue
            pending.append({
                "symbol": symbol,
                "relations": relations,
                "source_hash": source_hash,
                "eligible": eligible,
                "model_fingerprint": item_fingerprint,
            })

        generated = 0
        fallback = 0
        diagnostics = set()
        agent_failures = 0
        batch_size = max(1, min(16, CODE_FUNCTION_SUMMARY_BATCH_SIZE))
        for start in range(0, len(pending), batch_size):
            if self._cancelled and self._cancelled():
                raise AgentCancelledError("代码功能摘要已取消")
            batch = pending[start:start + batch_size]
            eligible_batch = [item for item in batch if item["eligible"]]
            summaries = {}
            evidence = {}
            if llm is not None and eligible_batch:
                try:
                    summaries = self._generate_summary_batch(llm, eligible_batch)
                except Exception:
                    diagnostic = "INJECTED_LLM_FAILED"
                    llm = None
            elif summarizer is not None and eligible_batch:
                try:
                    items = [{
                        "stable_key": item["symbol"]["stable_key"], "path": item["symbol"]["path"],
                        "kind": item["symbol"]["kind"], "qualified_name": item["symbol"]["qualified_name"],
                        "signature": item["symbol"].get("signature") or "", "relations": item["relations"],
                        "source_excerpt": (item["symbol"].get("source_excerpt") or "")[:CODE_FUNCTION_SUMMARY_MAX_SOURCE_CHARS],
                        "document_id": (item["symbol"].get("metadata") or {}).get("document_source", {}).get("document_id", ""),
                    } for item in eligible_batch]
                    resolved = summarizer.summarize(items, snapshot_id)
                    summaries = {key: value["summary"] for key, value in resolved.items()}
                    evidence = {key: value["evidence"] for key, value in resolved.items()}
                except AgentCancelledError:
                    raise
                except Exception as exc:
                    diagnostic = getattr(exc, "code", "AGENT_SUMMARY_FAILED")
                    summarizer = None
            for item in batch:
                symbol = item["symbol"]
                summary = summaries.get(symbol["stable_key"])
                origin = ("llm" if mode == "injected_llm" else "agent") if summary else "fallback"
                method = mode if summary else "deterministic"
                item_diagnostic = diagnostic if item["eligible"] else "outside_agent_scope"
                methods.add(method)
                if not summary:
                    summary = self._fallback_summary(symbol, item["relations"])
                    fallback += 1
                    diagnostics.add(item_diagnostic or "AGENT_INVALID_OUTPUT")
                    if mode == "agent" and item["eligible"]:
                        agent_failures += 1
                else:
                    generated += 1
                summary = summary.strip()[:CODE_FUNCTION_SUMMARY_MAX_CHARS]
                metadata = dict(symbol.get("metadata") or {})
                metadata.update({
                    "functional_summary": summary,
                    "functional_summary_source_hash": item["source_hash"],
                    "functional_summary_model": item["model_fingerprint"],
                    "functional_summary_version": _SUMMARY_VERSION,
                    "functional_summary_origin": origin,
                    "summary_method": method,
                    "functional_summary_diagnostic": None if origin != "fallback" else item_diagnostic,
                    "functional_summary_evidence": evidence.get(symbol["stable_key"], []),
                    "functional_summary_snapshot_id": snapshot_id,
                })
                self.repository.update_symbol_metadata(symbol["id"], metadata)
                symbol["metadata"] = metadata
                symbol["functional_summary"] = summary
        if pending:
            if self._cancelled and self._cancelled():
                self.db.rollback()
                raise AgentCancelledError("代码功能摘要已取消")
            self.db.commit()
        return {
            "summaries_generated": generated,
            "summaries_reused": reused,
            "summaries_fallback": fallback,
            "summary_method": next(iter(methods)) if len(methods) == 1 else "mixed",
            "summary_diagnostics": sorted(diagnostics),
            "summary_agent_failures": agent_failures,
            "summary_scope_skipped": scope_skipped,
            "summary_agent_usage": list(self._summary_usage),
        }

    def _record_summary_event(self, event):
        if event.get("event") == "usage" or event.get("type") == "usage":
            self._summary_usage.append(dict(event))
        if self._on_event is not None:
            self._on_event(event)

    @staticmethod
    def _vector_content_hash(content, kind, symbol):
        if kind == _FUNCTION_VECTOR_KIND:
            metadata = symbol.get("metadata") or {}
            content = json.dumps({"content": content, "summary_version": metadata.get("functional_summary_version"),
                                  "summary_method": metadata.get("summary_method"),
                                  "summary_model": metadata.get("functional_summary_model")}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

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
            "max_source_chars": CODE_FUNCTION_SUMMARY_MAX_SOURCE_CHARS,
            "max_summary_chars": CODE_FUNCTION_SUMMARY_MAX_CHARS,
            "relation_limit": CODE_FUNCTION_SUMMARY_RELATION_LIMIT,
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
                "document_id": (symbol.get("metadata") or {}).get("document_source", {}).get("document_id", ""),
                "path": symbol["path"],
                "language": symbol["language"],
                "kind": symbol["kind"],
                "qualified_name": symbol["qualified_name"],
                "vector_kind": vector_kind,
                "summary_method": (symbol.get("metadata") or {}).get("summary_method", "unknown"),
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
