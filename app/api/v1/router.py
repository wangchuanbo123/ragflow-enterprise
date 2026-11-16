"""API v1 路由聚合。"""

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    backups,
    code_graph,
    conversations,
    documents,
    files,
    health,
    graph_governance,
    graph_filters,
    index_jobs,
    knowledge_bases,
    knowledge_graph,
    messages,
    users,
    agent_runs,
    agent_tools,
    operations,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(agent_runs.router)
api_router.include_router(agent_tools.router)
api_router.include_router(backups.router)
api_router.include_router(conversations.router)
api_router.include_router(messages.router)
api_router.include_router(knowledge_bases.router)
api_router.include_router(documents.router)
api_router.include_router(files.router)
api_router.include_router(index_jobs.router)
api_router.include_router(knowledge_graph.router)
api_router.include_router(code_graph.router)
api_router.include_router(graph_governance.router)
api_router.include_router(graph_filters.router)
api_router.include_router(operations.router)
