"""Retriever over active code symbols and call/dependency edges."""

from __future__ import annotations

from time import perf_counter
from rag.retrievers.diagnostics import RetrievalDocuments, diagnostic

from langchain_core.documents import Document

from app.core.config import CODE_SEARCH_K
from rag.retrieval_strategy import get_retrieval_policy


class CodeGraphRetriever:
    def __init__(self, knowledge_base_id: str):
        self.knowledge_base_id = knowledge_base_id

    def get_relevant_documents(self, query: str):
        return self._retrieve(query, CODE_SEARCH_K)

    def retrieve_with_options(
        self,
        queries: list[str],
        *,
        query_embedding: list[float] | None = None,
        strategy: str = "standard",
        allowed_document_ids: set[str] | None = None,
    ):
        if allowed_document_ids is not None and not allowed_document_ids:
            return []
        policy = get_retrieval_policy(strategy)
        return self._retrieve(
            " ".join(dict.fromkeys(queries)),
            policy.code_k,
            query_embedding=query_embedding,
            allowed_document_ids=allowed_document_ids,
        )

    def _retrieve(
        self,
        query: str,
        limit: int,
        query_embedding: list[float] | None = None,
        allowed_document_ids: set[str] | None = None,
    ):
        from app.core.database import SessionLocal
        from app.repositories.code_graph_repository import CodeGraphRepository
        from app.services.code_vector_service import CodeVectorService

        started = perf_counter()
        status, code = "ok", "retrieved"
        diagnostics = []
        with SessionLocal() as db:
            repository = CodeGraphRepository(db, self.knowledge_base_id)
            active_snapshot = repository.active_snapshot()
            revision = active_snapshot.revision if active_snapshot else ""
            try:
                if query_embedding is not None and not len(query_embedding):
                    raise ValueError("shared_embedding_unavailable")
                symbols = CodeVectorService(
                    db, self.knowledge_base_id
                ).hybrid_search(
                    query,
                    limit,
                    query_embedding=query_embedding,
                    diagnostics=diagnostics,
                    **({"allowed_document_ids": allowed_document_ids}
                       if allowed_document_ids is not None else {}),
                )
            except Exception as exc:
                status, code = "degraded", "shared_embedding_unavailable" if query_embedding is not None and not len(query_embedding) else "vector_" + type(exc).__name__
                symbols = repository.search_symbols(
                    query, limit, allowed_document_ids=allowed_document_ids,
                )
            from app.models.knowledge_document import KnowledgeDocument
            from sqlalchemy import select

            source_ids = {
                (item.get("metadata") or {}).get("document_source", {}).get("document_id")
                for item in symbols
            } - {None, ""}
            source_documents = {
                document.id: document
                for document in db.scalars(select(KnowledgeDocument).where(
                    KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
                    KnowledgeDocument.id.in_(source_ids),
                    KnowledgeDocument.status == "ready",
                ))
            } if source_ids else {}
            symbols = [
                item for item in symbols
                if not (item.get("metadata") or {}).get("document_source")
                or (item["metadata"]["document_source"].get("document_id") in source_documents)
            ]
            relations = repository.symbol_relations(
                [item["id"] for item in symbols], limit=limit * 4
            )
        relations_by_symbol: dict[str, list[dict]] = {}
        for relation in relations:
            relations_by_symbol.setdefault(relation["source"], []).append(relation)
            if relation["target"]:
                relations_by_symbol.setdefault(relation["target"], []).append(relation)
        documents = []
        for symbol in symbols:
            facts = relations_by_symbol.get(symbol["id"], [])[:8]
            fact_lines = [
                f'{item["source_name"]} --{item["relation_type"]}--> {item["target_name"]}'
                for item in facts
            ]
            content = "\n".join([
                f'{symbol["kind"]} {symbol["qualified_name"]}',
                symbol.get("signature") or "",
                (
                    f'功能摘要：{symbol["functional_summary"]}'
                    if symbol.get("functional_summary")
                    else ""
                ),
                symbol.get("source_excerpt") or "",
                *fact_lines,
            ]).strip()
            document_source = (symbol.get("metadata") or {}).get("document_source") or {}
            original_document = source_documents.get(document_source.get("document_id"))
            source_excerpt = symbol.get("source_excerpt") or ""
            code_snippets = []
            if source_excerpt:
                code_snippets.append({
                    "content": source_excerpt,
                    "language": symbol["language"],
                    "path": symbol["path"],
                    "start_line": symbol["start_line"],
                    "end_line": symbol["end_line"],
                    "line_scope": "file",
                    "revision": revision,
                    "origin": "document" if document_source else "repository",
                    "document_id": document_source.get("document_id", ""),
                    "chunk_index": document_source.get("chunk_index"),
                    "symbol_id": symbol["id"],
                    "qualified_name": symbol["qualified_name"],
                    "is_verbatim": True,
                })
            documents.append(Document(page_content=content, metadata={
                "source": original_document.source_path if original_document else symbol["path"],
                "knowledge_base_id": self.knowledge_base_id,
                "document_id": document_source.get("document_id") or f'code:{symbol["snapshot_id"]}',
                "chunk_id": f'code-symbol:{symbol["id"]}',
                "symbol_id": symbol["id"],
                "qualified_name": symbol["qualified_name"],
                "language": symbol["language"],
                "start_line": symbol["start_line"],
                "end_line": symbol["end_line"],
                "code_relations": facts,
                "retrieval_score": symbol.get("retrieval_score"),
                "retrieval_methods": symbol.get("retrieval_methods", ["lexical"]),
                "vector_match_types": symbol.get("vector_match_types", []),
                "functional_summary": symbol.get("functional_summary"),
                "summary_method": (symbol.get("metadata") or {}).get("summary_method"),
                "summary_diagnostic": (symbol.get("metadata") or {}).get("functional_summary_diagnostic"),
                "contains_code": bool(source_excerpt),
                "code_snippets": code_snippets,
                "_channel": "code",
            }))
        return RetrievalDocuments(documents, diagnostics=[*diagnostics, diagnostic("code", "degraded" if diagnostics else status, code, len(documents), (perf_counter()-started)*1000)])


def get_code_graph_retriever(knowledge_base_id: str):
    from app.core.database import SessionLocal
    from app.repositories.code_graph_repository import CodeGraphRepository

    with SessionLocal() as db:
        if CodeGraphRepository(db, knowledge_base_id).active_snapshot() is None:
            return None
    return CodeGraphRetriever(knowledge_base_id)
