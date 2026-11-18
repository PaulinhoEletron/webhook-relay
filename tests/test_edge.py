import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api.middleware import RequestBoundary
from app.main import create_app

REQUIRED = (
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "permissions-policy",
    "cross-origin-opener-policy",
    "cross-origin-resource-policy",
)


def assert_headers(response):
    for name in REQUIRED:
        assert name in response.headers, name
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


def test_headers_on_every_kind_of_response(client, headers):
    responses = [
        client.get("/"),
        client.get("/health/live"),
        client.get("/v1/events/x", headers=headers),
        client.get("/v1/events/x"),
        client.get("/nope"),
        client.patch("/"),
        client.post("/v1/events", content=b"x" * 20000, headers=headers),
        client.post("/demo/runs", json={"scenario": "bad"}),
    ]
    assert [r.status_code for r in responses][4:7] == [404, 405, 413]
    for response in responses:
        assert_headers(response)
    strict = (
        "default-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    assert responses[1].headers["content-security-policy"] == strict
    assert responses[4].headers["content-security-policy"] == strict
    # The demo page keeps its own stricter policy and the header is not duplicated.
    page = responses[0]
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert len(page.headers.get_list("content-security-policy")) == 1
    assert len(page.headers.get_list("cache-control")) == 1


def test_no_store_scope(client, headers):
    for path in ("/health/live", "/v1/events/x", "/demo/runs/x", "/health/worker"):
        assert client.get(path, headers=headers).headers["cache-control"] == "no-store"
    assert "cache-control" not in client.get("/openapi.json").headers


def test_docs_policy_is_looser_only_for_docs(client):
    docs = client.get("/docs")
    assert docs.status_code == 200
    csp = docs.headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net" in csp and "https://fastapi.tiangolo.com" in csp
    assert "frame-ancestors 'none'" in csp
    assert "cdn.jsdelivr.net" not in client.get("/openapi.json").headers["content-security-policy"]
    assert client.get("/redoc").status_code == 404


def test_unknown_paths_and_methods_are_json(client):
    missing = client.get("/does-not-exist")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "http_error"
    assert client.get("/source").status_code == 404
    for method in ("PATCH", "PUT", "DELETE", "OPTIONS", "TRACE"):
        response = client.request(method, "/")
        assert response.status_code == 405, method
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["error"]["request_id"]
    for path in ("/", "/health/live", "/health/ready", "/health/worker", "/v1/events/x"):
        assert client.head(path).status_code in (200, 401, 405)
    for path in ("/health/live", "/health/ready", "/health/worker"):
        head = client.head(path)
        assert head.status_code == client.get(path).status_code, path
        assert head.content == b""
        assert head.headers["cache-control"] == "no-store"
        assert_headers(head)
    assert client.head("/health/live").status_code == 200
    assert client.head("/").status_code == 405


def test_unexpected_errors_are_json_with_headers(app):
    @app.get("/boom")
    def boom():
        raise RuntimeError("secret detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret detail" not in response.text
    assert_headers(response)


def asgi(timeout):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return RequestBoundary(inner, body_timeout=timeout)


def test_stalled_body_gets_408():
    sent = []

    async def run():
        queue = asyncio.Queue()
        await queue.put({"type": "http.request", "body": b"0123456789", "more_body": True})

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/v1/events", "headers": []}
        await asgi(0.2)(scope, queue.get, send)

    asyncio.run(asyncio.wait_for(run(), 5))
    assert sent[0]["status"] == 408
    names = {name for name, _ in sent[0]["headers"]}
    assert b"content-security-policy" in names and b"x-request-id" in names
    assert b"request_timeout" in sent[1]["body"]


def test_prompt_body_is_not_affected_by_deadline():
    sent = []

    async def run():
        queue = asyncio.Queue()
        await queue.put({"type": "http.request", "body": b"{}", "more_body": False})

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
        await asgi(0.2)(scope, queue.get, send)

    asyncio.run(run())
    assert sent[0]["status"] == 204


def test_body_read_timeout_wired_from_settings(settings):
    app = create_app(settings.model_copy(update={"body_read_timeout": 3}))
    middleware = next(m for m in app.user_middleware if m.cls is RequestBoundary)
    assert middleware.kwargs["body_timeout"] == 3


@pytest.mark.parametrize("value", [0, -1, 61])
def test_body_timeout_bounds(settings, value):
    with pytest.raises(ValueError):
        type(settings)(**{**settings.model_dump(), "body_read_timeout": value})


def test_cloudflare_header_gives_separate_quota_buckets(settings):
    from sqlalchemy import text

    app = create_app(settings.model_copy(update={"trust_cf_connecting_ip": True}))
    with app.state.sessions.begin() as session:
        session.execute(text("TRUNCATE events, fulfillment_effects CASCADE"))

    def create(ip):
        return TestClient(app).post(
            "/demo/runs",
            json={"scenario": "success"},
            headers={"CF-Connecting-IP": ip, "X-Forwarded-For": "198.51.100.1"},
        )

    assert [create("192.0.2.1").status_code for _ in range(6)] == [202] * 5 + [429]
    assert create("192.0.2.2").status_code == 202
