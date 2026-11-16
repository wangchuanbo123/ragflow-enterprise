"""Knowledge workbench lifecycle endpoints; runtime tools use a separate router."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.models.agent_run import AgentRun
from app.schemas.agent_run import AgentRunCreate
from app.services.agent_workflow_service import AgentWorkflowService
from app.services.knowledge_workflows.catalog import task_catalog
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from app.schemas.agent_run import Task
from typing import Literal

router = APIRouter(prefix="/knowledge-bases", tags=["agent-runs"])


@router.get("/{knowledge_base_id}/agent-task-catalog")
def catalog(knowledge_base_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    service = AgentWorkflowService(db, knowledge_base_id)
    service._authorize(user.id, edit=False)
    return task_catalog()


@router.post("/{knowledge_base_id}/agent-runs", status_code=202)
def create_run(knowledge_base_id: str, payload: AgentRunCreate,
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    service = AgentWorkflowService(db, knowledge_base_id)
    return service.serialize(service.create(payload, user.id))


@router.get("/{knowledge_base_id}/agent-runs")
def list_runs(knowledge_base_id: str, limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0),
              user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    service = AgentWorkflowService(db, knowledge_base_id)
    service._authorize(user.id, edit=False)
    runs = list(db.scalars(select(AgentRun).where(AgentRun.knowledge_base_id == knowledge_base_id)
                         .order_by(AgentRun.created_at.desc(), AgentRun.id).offset(offset).limit(limit)))
    return [service.serialize(service.get(run.id), events=False) for run in runs]


@router.get("/{knowledge_base_id}/agent-runs/page")
def list_run_page(knowledge_base_id: str, limit: int = Query(default=50, ge=1, le=100),
                  offset: int = Query(default=0, ge=0), task: Task | None = None,
                  status: Literal['queued', 'running', 'succeeded', 'failed', 'cancelled'] | None = None,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    service = AgentWorkflowService(db, knowledge_base_id)
    service._authorize(user.id, edit=False)
    conditions = [AgentRun.knowledge_base_id == knowledge_base_id]
    if task:
        conditions.append(AgentRun.task == task)
    if status:
        conditions.append(AgentRun.status == status)
    total = db.scalar(select(func.count()).select_from(AgentRun).where(*conditions)) or 0
    runs = list(db.scalars(select(AgentRun).where(*conditions)
                         .order_by(AgentRun.created_at.desc(), AgentRun.id).offset(offset).limit(limit)))
    return {'items': [service.serialize(run, events=False) for run in runs],
            'total': total, 'offset': offset, 'limit': limit}


@router.get("/{knowledge_base_id}/agent-runs/{run_id}")
def get_run(knowledge_base_id: str, run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    service = AgentWorkflowService(db, knowledge_base_id)
    service._authorize(user.id, edit=False)
    return service.serialize(service.get(run_id))


@router.post("/{knowledge_base_id}/agent-runs/{run_id}/cancel")
def cancel_run(knowledge_base_id: str, run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    service = AgentWorkflowService(db, knowledge_base_id)
    return service.serialize(service.cancel(run_id, user.id))


@router.get("/{knowledge_base_id}/agent-runs/{run_id}/artifact")
def download_artifact(knowledge_base_id: str, run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    service = AgentWorkflowService(db, knowledge_base_id)
    service._authorize(user.id, edit=False)
    run = service.get(run_id)
    return JSONResponse(jsonable_encoder(service.serialize(run)), headers={
        "Content-Disposition": f'attachment; filename="{run.task}-{run.id}.json"', "Cache-Control": "private, no-store"})


@router.post("/{knowledge_base_id}/agent-runs/{run_id}/retry", status_code=202)
def retry_run(knowledge_base_id: str, run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    service = AgentWorkflowService(db, knowledge_base_id)
    return service.serialize(service.retry(run_id, user.id))
