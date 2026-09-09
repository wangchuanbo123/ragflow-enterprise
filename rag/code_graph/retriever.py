"""Retriever over active code symbols and call/dependency edges."""

from __future__ import annotations

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
        del allowed_document_ids
        policy = get_retrieval_policy(strategy)
        return self._retrieve(
            " ".join(dict.fromkeys(queries)),
            policy.code_k,
            query_embedding=query_embedding,
        )

    def _retrieve(
        self,
        query: str,
        limit: int,
        query_embedding: list[float] | None = None,
    ):
        from app.core.database import SessionLocal
        from app.repositories.code_graph_repository import CodeGraphRepository
        from app.services.code_vector_service import CodeVectorService

        with SessionLocal() as db:
            repository = CodeGraphRepository(db, self.knowledge_base_id)
            active_snapshot = repository.active_snapshot()
            revision = active_snapshot.revision if active_snapshot else ""
            try:
                symbols = CodeVectorService(
                    db, self.knowledge_base_id
                ).hybrid_search(
                    query,
                    limit,
                    query_embedding=query_embedding,
                )
            except Exception:
                symbols = repository.search_symbols(query, limit)
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
                "source": symbol["path"],
                "knowledge_base_id": self.knowledge_base_id,
                "document_id": f'code:{symbol["snapshot_id"]}',
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
                "contains_code": bool(source_excerpt),
                "code_snippets": code_snippets,
                "_channel": "code",
            }))
        return documents


def get_code_graph_retriever(knowledge_base_id: str):
    from app.core.database import SessionLocal
    from app.repositories.code_graph_repository import CodeGraphRepository

    with SessionLocal() as db:
        if CodeGraphRepository(db, knowledge_base_id).active_snapshot() is None:
            return None
    return CodeGraphRetriever(knowledge_base_id)
