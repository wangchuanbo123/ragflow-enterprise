"""
FastAPI接口
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.models.user import User
from app.services.rag_service import ask_question


router = APIRouter() # 创建一个 FastAPI 的 APIRouter 实例

class QueryRequest(BaseModel):  # BaseModel是 Pydantic 提供的一个基类，用于定义数据模型和验证输入数据的结构和类型

    query: str


@router.post("/ask")  # 当客户端发送一个 POST 请求到这个路径时，FastAPI 会调用函数来处理这个请求

def ask(
    request: QueryRequest,
    current_user: User = Depends(get_current_user),
):

    del current_user
    result = ask_question(request.query)

    return result
