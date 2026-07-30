"""
FastAPI 入口

- 保留原有 /ask 接口
- 新增 /api/v1 下的鉴权、会话、消息、流式、文档管理与健康检查接口
- 启动时执行数据库迁移并按需创建初始管理员
- X-Request-ID 中间件
"""

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.errors import (
    APIError,
    api_error_handler,
    http_exception_handler,
    rag_error_handler,
    validation_exception_handler,
)
from app.api.routes import router as legacy_router
from app.api.v1.router import api_router
from app.core.config import CORS_ORIGINS


class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每个请求分配或传递 X-Request-ID。"""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.init_db import init_app
    from app.core.logger import setup_logging

    setup_logging()

    # Recover stale index jobs
    try:
        from app.core.database import SessionLocal
        from app.repositories.index_job_repository import IndexJobRepository
        with SessionLocal() as db:
            recovered = IndexJobRepository(db).recover_stale_jobs()
            if recovered:
                import logging
                logging.getLogger(__name__).info("Recovered %d stale index jobs", recovered)
    except Exception:
        pass

    init_app()
    try:
        yield
    finally:
        from app.services.index_worker import stop_worker

        stop_worker()


app = FastAPI(lifespan=lifespan)

app.add_middleware(RequestIDMiddleware)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.add_exception_handler(APIError, api_error_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)

from rag.errors import RAGError  # noqa: E402
app.add_exception_handler(RAGError, rag_error_handler)

app.include_router(legacy_router)  # 原有 /ask
app.include_router(api_router)  # 新增 /api/v1


def _mount_static(app: FastAPI) -> None:
    """若存在前端构建产物 web/dist，则由 FastAPI 直接托管。"""
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    dist = Path(__file__).resolve().parent.parent / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="static")


_mount_static(app)
