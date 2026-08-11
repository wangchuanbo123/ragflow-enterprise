"""Stable identifiers used by the legacy single-knowledge-base compatibility layer."""

DEFAULT_KNOWLEDGE_BASE_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_KNOWLEDGE_BASE_SLUG = "default"
DEFAULT_KNOWLEDGE_BASE_NAME = "默认知识库"


def object_prefix_for_knowledge_base(knowledge_base_id: str) -> str:
    """Keep existing local object keys intact while isolating newly created KBs."""
    if knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID:
        return ""
    return f"knowledge-bases/{knowledge_base_id}"

