"""统一错误响应与异常。"""

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from rag.errors import RAGError


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def error_response(
    code: str, message: str, status_code: int = status.HTTP_400_BAD_REQUEST
) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=_error(code, message))


class APIError(HTTPException):
    def __init__(self, code: str, message: str, status_code: int = status.HTTP_400_BAD_REQUEST):
        super().__init__(status_code=status_code, detail=_error(code, message))
        self.code = code
        self.message = message


def unauthorized(message: str = "未登录") -> APIError:
    return APIError("UNAUTHORIZED", message, status.HTTP_401_UNAUTHORIZED)


def forbidden(message: str = "无权访问") -> APIError:
    return APIError("FORBIDDEN", message, status.HTTP_403_FORBIDDEN)


def not_found(message: str = "资源不存在") -> APIError:
    return APIError("NOT_FOUND", message, status.HTTP_404_NOT_FOUND)


def bad_request(message: str = "请求参数错误") -> APIError:
    return APIError("BAD_REQUEST", message, status.HTTP_400_BAD_REQUEST)


async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:  # noqa: ARG001
    return error_response(exc.code, exc.message, status_code=exc.status_code)


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:  # noqa: ARG001
    if isinstance(exc.detail, dict) and exc.detail.get("error"):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return error_response("HTTP_ERROR", str(exc.detail), status_code=exc.status_code)


async def rag_error_handler(request: Request, exc: RAGError) -> JSONResponse:  # noqa: ARG001
    return error_response(exc.code, exc.message, status_code=exc.status_code)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError  # noqa: ARG001
) -> JSONResponse:
    return error_response(
        "VALIDATION_ERROR", "请求参数校验失败", status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
    )
