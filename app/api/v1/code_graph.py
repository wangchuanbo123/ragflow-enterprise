"""Code symbol, call, and dependency graph APIs."""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import bad_request, forbidden, not_found
from app.core.database import get_db
from app.models.user import User
from app.repositories.code_graph_repository import CodeGraphRepository
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.schemas.code_graph import (
    CodeGraphStatsOut,
    CodeIndexRequest,
    CodeRelationOut,
    CodeSnapshotOut,
    CodeSubgraphOut,
    CodeSymbolOut,
)
from app.schemas.document import IndexJobOut
from app.services.code_index_service import CodeIndexService

router = APIRouter(prefix="/knowledge-bases", tags=["code-graph"])


def _require(db: Session, knowledge_base_id: str, user: User, *, edit: bool = False) -> None:
    repository = KnowledgeBaseRepository(db)
    allowed = (
        repository.can_edit(knowledge_base_id, user.id, is_admin=user.role == "admin")
        if edit else
        repository.can_read(knowledge_base_id, user.id, is_admin=user.role == "admin")
    )
    if not allowed:
        if repository.get(knowledge_base_id) is None:
            raise not_found("知识库不存在")
        raise forbidden("没有该知识库的访问权限")


@router.get("/{knowledge_base_id}/code-graph/stats", response_model=CodeGraphStatsOut)
def code_graph_stats(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    return CodeGraphRepository(db, knowledge_base_id).stats()


@router.get("/{knowledge_base_id}/code-graph/snapshots", response_model=list[CodeSnapshotOut])
def list_code_snapshots(
    knowledge_base_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    return CodeGraphRepository(db, knowledge_base_id).list_snapshots(limit)


@router.post(
    "/{knowledge_base_id}/code-graph/index",
    response_model=IndexJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_code_index(
    knowledge_base_id: str,
    payload: CodeIndexRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user, edit=True)
    try:
        return CodeIndexService(db, knowledge_base_id).request_index(
            payload.data_source_id,
            current_user.id,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.get("/{knowledge_base_id}/code-graph/symbols", response_model=list[CodeSymbolOut])
def search_code_symbols(
    knowledge_base_id: str,
    query: str = Query(min_length=1, max_length=255),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    return CodeGraphRepository(db, knowledge_base_id).search_symbols(query, limit)


@router.get("/{knowledge_base_id}/code-graph/subgraph", response_model=CodeSubgraphOut)
def get_code_subgraph(
    knowledge_base_id: str,
    symbol_ids: list[str] = Query(min_length=1, max_length=20),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    repository = CodeGraphRepository(db, knowledge_base_id)
    nodes = [item for symbol_id in symbol_ids if (item := repository.get_symbol(symbol_id))]
    edges = repository.symbol_relations(symbol_ids, limit)
    related_ids = {
        value
        for edge in edges
        for value in (edge["source"], edge["target"])
        if value and value not in symbol_ids
    }
    nodes.extend(
        item for symbol_id in sorted(related_ids)
        if (item := repository.get_symbol(symbol_id))
    )
    return CodeSubgraphOut(nodes=nodes, edges=edges)
