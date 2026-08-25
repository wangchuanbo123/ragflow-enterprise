"""知识图谱 GraphStore 抽象接口。"""

from __future__ import annotations

from typing import Any, Protocol


class GraphStore(Protocol):
    def upsert_extraction(self, chunk: Any, extraction: dict) -> None:
        """保存一个 chunk 的实体关系抽取结果。"""
        ...

    def delete_document_graph(self, document_id: str) -> None:
        """删除文档关联的全部图谱数据。"""
        ...

    def search_entities(self, query: str, limit: int) -> list[dict]:
        """通过精确/包含匹配查询实体。"""
        ...

    def search_relations(self, query: str, limit: int) -> list[dict]:
        """通过关系谓词及其别名匹配关系。"""
        ...

    def get_relations(self, relation_ids: list[str]) -> list[dict]:
        """按 ID 获取应用人工治理覆盖后的关系。"""
        ...

    def get_neighbors(
        self,
        entity_ids: list[str],
        max_hops: int,
        limit: int,
        offset: int = 0,
    ) -> list[dict]:
        """获取稳定分页的实体邻居关系。"""
        ...

    def get_relation_evidence(self, relation_ids: list[str]) -> list[dict]:
        """获取关系的原文证据。"""
        ...

    def get_document_subgraph(self, document_id: str, limit: int) -> dict:
        """Return the evidence-backed subgraph originating from one document."""
        ...

    def cleanup_orphans(self) -> None:
        """清理无提及、无关系的孤立实体和无证据关系。"""
        ...

    def stats(self) -> dict:
        """返回图谱统计信息。"""
        ...
