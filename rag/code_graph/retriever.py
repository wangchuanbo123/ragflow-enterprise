"""Retriever over active code symbols and call/dependency edges."""

from __future__ import annotations

from langchain_core.documents import Document

from app.core.config import CODE_SEARCH_K


class CodeGraphRetriever:
    def __init__(self, knowledge_base_id: str):
        self.knowledge_base_id = knowledge_base_id

    def get_relevant_documents(self, query: str):
        from app.core.database import SessionLocal
        from app.repositories.code_graph_repository import CodeGraphRepository

        with SessionLocal() as db:
            repository = CodeGraphRepository(db, self.knowledge_base_id)
            symbols = repository.search_symbols(query, CODE_SEARCH_K)
            relations = repository.symbol_relations([item["id"] for item in symbols], limit=CODE_SEARCH_K * 4)
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
                symbol.get("source_excerpt") or "",
                *fact_lines,
            ]).strip()
            documents.append(Document(page_content=content, metadata={
                "source": symbol["path"],
                "document_id": f'code:{symbol["snapshot_id"]}',
                "chunk_id": f'code-symbol:{symbol["id"]}',
                "symbol_id": symbol["id"],
                "qualified_name": symbol["qualified_name"],
                "language": symbol["language"],
                "start_line": symbol["start_line"],
                "end_line": symbol["end_line"],
                "code_relations": facts,
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
