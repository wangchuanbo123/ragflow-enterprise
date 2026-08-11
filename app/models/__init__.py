"""
SQLAlchemy ORM 模型基类导入聚合
"""

from app.core.database import Base
from app.models.code_graph import (
    CodeFile,
    CodeRelation,
    CodeSnapshot,
    CodeSymbol,
    EntityVectorIndexState,
)
from app.models.conversation import Conversation
from app.models.document_management import (
    DocumentTag,
    DocumentTagLink,
    DocumentVersion,
    KnowledgeFolder,
)
from app.models.index_job import IndexJob, IndexJobEvent, IndexJobItem, KnowledgeIndexState
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseMember,
    KnowledgeDataSource,
)
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.knowledge_graph import (
    KgAlias,
    KgEntity,
    KgEntityMention,
    KgGraphCorrection,
    KgRelation,
    KgRelationEvidence,
)
from app.models.graph_governance import (
    GraphCommunity,
    GraphCommunityMember,
    GraphExtractionCache,
    GraphQualityReport,
    GraphReviewItem,
    GraphSchemaDefinition,
    KgEntityMerge,
)
from app.models.message import Message
from app.models.user import User

__all__ = [
    "Base",
    "CodeSnapshot",
    "CodeFile",
    "CodeSymbol",
    "CodeRelation",
    "EntityVectorIndexState",
    "User",
    "Conversation",
    "Message",
    "KnowledgeFolder",
    "DocumentTag",
    "DocumentTagLink",
    "DocumentVersion",
    "KnowledgeBase",
    "KnowledgeBaseMember",
    "KnowledgeDataSource",
    "KnowledgeDocument",
    "KnowledgeChunk",
    "KnowledgeIndexState",
    "IndexJob",
    "IndexJobEvent",
    "IndexJobItem",
    "KgEntity",
    "KgAlias",
    "KgEntityMention",
    "KgGraphCorrection",
    "KgRelation",
    "KgRelationEvidence",
    "GraphSchemaDefinition",
    "GraphExtractionCache",
    "GraphQualityReport",
    "KgEntityMerge",
    "GraphReviewItem",
    "GraphCommunity",
    "GraphCommunityMember",
]
