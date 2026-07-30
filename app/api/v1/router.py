"""API v1 路由聚合。"""

from fastapi import APIRouter

from app.api.v1 import auth, conversations, documents, health, index_jobs, knowledge_graph, messages

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(conversations.router)
api_router.include_router(messages.router)
api_router.include_router(documents.router)
api_router.include_router(index_jobs.router)
api_router.include_router(knowledge_graph.router)
