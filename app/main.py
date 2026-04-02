"""
FastAPI入口

"""

from fastapi import FastAPI

from app.api.routes import router

app = FastAPI()

app.include_router(router)  #  将定义的 API 路由 router 包含到 FastAPI 中