"""ASGI timing includes streaming bodies; no request content enters telemetry."""
import time
import uuid
from app.services.observability import observe, operation_context


class OperationsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        supplied = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = supplied if supplied and len(supplied) <= 128 and all(c.isalnum() or c in "-_." for c in supplied) else str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        status, first = 500, True
        started = time.monotonic()
        async def measured_send(message):
            nonlocal status, first
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and first and message.get("body"):
                observe("http_first_body_seconds", time.monotonic() - started)
                first = False
            await send(message)
        with operation_context(request_id=request_id):
            try:
                await self.app(scope, receive, measured_send)
            finally:
                observe("http_requests_" + str(status // 100) + "xx")
                observe("http_request_seconds", time.monotonic() - started)
