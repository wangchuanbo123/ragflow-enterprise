"""Transactionally maintained graph version for safe, cheap cache validation."""

import uuid

from sqlalchemy import BigInteger, ForeignKey, String, event
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class KnowledgeGraphRevision(Base):
    __tablename__ = "knowledge_graph_revisions"

    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True)
    incarnation: Mapped[str] = mapped_column(String(32), nullable=False, default=lambda: uuid.uuid4().hex)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")


@event.listens_for(Base.metadata, "after_create")
def _revision_triggers(metadata, connection, **kwargs):
    from app.services.graph_revision import install_revision_triggers
    install_revision_triggers(connection)
