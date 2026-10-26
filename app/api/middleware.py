import json
import logging
import time
from uuid import uuid4

from starlette.responses import JSONResponse

logger = logging.getLogger("relay.api")
MAX_BODY = 16 * 1024


def error_response(code, message, request_id, status):
    return JSONResponse(
        {"error": {"code": code, "message": message, "request_id": str(request_id)}},
        status_code=status,
    )


class RequestBoundary:
    """Bound incoming bytes even when Content-Length is missing or inaccurate."""

    def __init__(self, app, max_body=MAX_BODY):
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = str(uuid4())
        started = time.monotonic()
        scope.setdefault("state", {})["request_id"] = request_id

        async def with_id(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
                logger.info(
                    json.dumps(
                        {
                            "request_id": request_id,
                            "status": message["status"],
                            "duration_ms": int((time.monotonic() - started) * 1000),
                        }
                    )
                )
            await send(message)

        async def reject(status, code, message):
            await error_response(code, message, request_id, status)(scope, receive, with_id)

        lengths = [v for k, v in scope["headers"] if k == b"content-length"]
        if lengths:
            try:
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise ValueError
                if int(lengths[0]) > self.max_body:
                    return await reject(413, "body_too_large", "Body exceeds size limit")
            except ValueError:
                return await reject(422, "invalid_request", "Invalid Content-Length")
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_body:
                return await reject(413, "body_too_large", "Body exceeds size limit")
            if not message.get("more_body", False):
                break
        consumed = False

        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, with_id)
