"""知识图谱查询 API。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import KNOWLEDGE_GRAPH_ENABLED
from app.core.database import get_db
from app.models.user import User
from app.schemas.knowledge_graph import (
    EntityOut,
    GraphStatsOut,
    NeighborRelationOut,
)
from rag.knowledge_graph.sqlite_store import SqliteGraphStore

router = APIRouter(prefix="/knowledge-graph", tags=["knowledge-graph"])


def _check_enabled():
    if not KNOWLEDGE_GRAPH_ENABLED:
        from app.api.errors import bad_request
        raise bad_request("知识图谱未启用")


@router.get("/stats", response_model=GraphStatsOut)
def graph_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_enabled()
    store = SqliteGraphStore(db)
    return GraphStatsOut(**store.stats())


@router.get("/entities", response_model=list[EntityOut])
def search_entities(
    query: str = Query(min_length=1, max_length=255),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_enabled()
    store = SqliteGraphStore(db)
    entities = store.search_entities(query, limit)
    return [EntityOut(**e) for e in entities]


@router.get("/entities/{entity_id}", response_model=EntityOut)
def get_entity(
    entity_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_enabled()
    from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository
    repo = KnowledgeGraphRepository(db)
    entity = repo.get_entity(entity_id)
    if not entity:
        from app.api.errors import not_found
        raise not_found("实体不存在")
    aliases = repo.get_entity_aliases(entity_id)
    return EntityOut(
        id=entity.id,
        entity_type=entity.entity_type,
        canonical_name=entity.canonical_name,
        normalized_name=entity.normalized_name,
        description=entity.description,
        aliases=[a.alias for a in aliases],
    )


@router.get("/entities/{entity_id}/neighbors", response_model=list[NeighborRelationOut])
def get_neighbors(
    entity_id: str,
    hops: int = Query(default=1, ge=1, le=2),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_enabled()
    store = SqliteGraphStore(db)
    relations = store.get_neighbors([entity_id], hops, limit)
    return [NeighborRelationOut(**r) for r in relations]
