import asyncio
import logging
import time
from uuid import uuid4

from starlette.responses import JSONResponse

from app.metrics import HTTP_DURATION, HTTP_REQUESTS

logger = logging.getLogger("relay.api")
UNMATCHED = "<unmatched>"
MAX_BODY = 16 * 1024
BODY_READ_TIMEOUT = 10.0

STRICT_CSP = (
    "default-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
# Swagger UI loads its bundle from jsDelivr and starts with an inline script.
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
        "microphone=(), payment=(), usb=()"
    ),
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}
NO_STORE_PREFIXES = ("/v1/", "/demo/", "/health/")


def edge_headers(path):
    """Headers every response carries unless the route already set its own value."""
    headers = dict(SECURITY_HEADERS)
    docs = path == "/docs" or path.startswith("/docs/")
    headers["content-security-policy"] = DOCS_CSP if docs else STRICT_CSP
    if path.startswith(NO_STORE_PREFIXES):
        headers["cache-control"] = "no-store"
    return headers


def error_response(code, message, request_id, status):
    return JSONResponse(
        {"error": {"code": code, "message": message, "request_id": str(request_id)}},
        status_code=status,
    )


class RequestBoundary:
    """Bound incoming bytes even when Content-Length is missing or inaccurate."""

    def __init__(self, app, max_body=MAX_BODY, client_id=None, body_timeout=BODY_READ_TIMEOUT):
        self.app = app
        self.max_body = max_body
        self.body_timeout = body_timeout
        # Returns an already-hashed client identifier for the access log.
        self.client_id = client_id

    def log_client(self, scope):
        try:
            return self.client_id(scope) if self.client_id else None
        except Exception:
            return "invalid"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = str(uuid4())
        started = time.monotonic()
        scope.setdefault("state", {})["request_id"] = request_id

        async def with_id(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {name.lower() for name, _ in headers}
                for name, value in edge_headers(scope.get("path", "")).items():
                    if name.encode() not in present:
                        headers.append((name.encode(), value.encode()))
                headers.append((b"x-request-id", request_id.encode()))
                elapsed = time.monotonic() - started
                route = getattr(scope.get("route"), "path", None) or UNMATCHED
                HTTP_REQUESTS.labels(route, str(message["status"])).inc()
                HTTP_DURATION.labels(route).observe(elapsed)
                logger.info(
                    "request",
                    extra={
                        "request_id": request_id,
                        "method": scope["method"],
                        "route": route,
                        "status": message["status"],
                        "duration_ms": int(elapsed * 1000),
                        "client": self.log_client(scope),
                    },
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
        try:
            async with asyncio.timeout(self.body_timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > self.max_body:
                        return await reject(413, "body_too_large", "Body exceeds size limit")
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "request_timeout", "Request body not received in time")
        consumed = False

        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        started_response = False

        async def tracked(message):
            nonlocal started_response
            started_response = started_response or message["type"] == "http.response.start"
            await with_id(message)

        try:
            await self.app(scope, bounded_receive, tracked)
        except Exception:
            # Outside this middleware Starlette would emit a bare 500 without edge headers.
            logger.exception("unhandled error", extra={"request_id": request_id})
            if started_response:
                raise
            await reject(500, "internal_error", "Unexpected server error")
