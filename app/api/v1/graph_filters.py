"""Personal, read-permission graph views; never fact or Schema mutations."""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.graph_filter import (GraphFilterPreviewRequest, GraphFilterPreviewResponse, GraphFilterSaveRequest,
                                      GraphFilterViewOut, GraphFilterLimits)
from app.services.graph_filter_service import GraphFilterService, GraphFilterError

router = APIRouter(prefix="/knowledge-bases/{knowledge_base_id}/knowledge-graph", tags=["graph-filter-views"])


def _call(function):
    try:
        return function()
    except GraphFilterError as exc:
        raise HTTPException(status_code=exc.status, detail={"error": {"code": exc.code, "message": exc.message,
            "details": exc.details}}) from None


@router.post("/filter-preview", response_model=GraphFilterPreviewResponse)
def preview_graph_filter(knowledge_base_id: str, payload: GraphFilterPreviewRequest,
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _call(lambda: GraphFilterService(db, knowledge_base_id, user.id).preview(payload))


@router.get("/filters", response_model=list[GraphFilterViewOut])
def saved_graph_filters(knowledge_base_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _call(lambda: GraphFilterService(db, knowledge_base_id, user.id).list_saved())


@router.post("/filters", response_model=GraphFilterViewOut, status_code=201)
def save_graph_filter(knowledge_base_id: str, payload: GraphFilterSaveRequest,
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _call(lambda: GraphFilterService(db, knowledge_base_id, user.id).save(payload))


@router.post("/filters/{filter_id}/preview", response_model=GraphFilterPreviewResponse)
def preview_saved_graph_filter(knowledge_base_id: str, filter_id: str, payload: GraphFilterLimits,
                              user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _call(lambda: GraphFilterService(db, knowledge_base_id, user.id).preview_saved(filter_id, payload))


@router.delete("/filters/{filter_id}", status_code=204)
def delete_graph_filter(knowledge_base_id: str, filter_id: str,
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _call(lambda: GraphFilterService(db, knowledge_base_id, user.id).delete(filter_id))
    return Response(status_code=204)
