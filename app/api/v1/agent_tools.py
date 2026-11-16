"""Minimal MCP Streamable HTTP JSON-RPC endpoint with run-only bearer auth."""

import json
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.api.errors import unauthorized
from app.services.knowledge_workflows.tools import authorize_tool_token, call_tool, tool_catalog

router = APIRouter(tags=["agent-tools"])


@router.post("/agent-tools")
async def agent_tools(request: Request, db: Session = Depends(get_db)):
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise unauthorized("需要代理工具令牌")
    try:
        run = authorize_tool_token(db, authorization[7:])
    except PermissionError as exc:
        raise unauthorized(str(exc)) from exc
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 65536:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "请求过大"}}
    try:
        payload = json.loads(data)
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise ValueError()
    except (ValueError, TypeError):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "无效 JSON-RPC 请求"}}
    identifier = payload.get("id")
    method = payload.get("method")
    if method == "notifications/initialized":
        return Response(status_code=204)
    try:
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "rag-enterprise-scoped-tools", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": tool_catalog()}
        elif method == "tools/call":
            params = payload.get("params") or {}
            value = call_tool(db, run, params.get("name", ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "未支持的方法"}}
    except Exception:
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32602, "message": "参数无效或超出任务授权范围"}}
    return {"jsonrpc": "2.0", "id": identifier, "result": result}
