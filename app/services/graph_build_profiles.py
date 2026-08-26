"""Build profiles mapping fast/standard/deep strategies to job options."""

from __future__ import annotations

GRAPH_BUILD_PROFILES: dict[str, dict] = {
    # 快速：只抽新增/变化 Chunk，向量增量更新，社区延迟重建（实时上传）
    "fast": {
        "mode": "pending",
        "vector_update": "incremental",
        "community_update": "defer",
    },
    # 标准：增量抽取并重试失败 Chunk，向量增量更新，只重建受影响子图（日常使用）
    "standard": {
        "mode": "retry_failed",
        "vector_update": "incremental",
        "community_update": "affected",
    },
    # 深度：全量重新抽取，向量全量重建，社区全量重建（Schema 大改、离线维护）
    "deep": {
        "mode": "full",
        "vector_update": "full",
        "community_update": "full",
    },
}

VALID_COMMUNITY_UPDATES = {"defer", "affected", "full"}
VALID_VECTOR_UPDATES = {"none", "incremental", "full"}
