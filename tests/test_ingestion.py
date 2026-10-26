import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.api.middleware import MAX_BODY
from app.db.models import Delivery, Event
from app.main import create_app

PAYLOAD = {"event_type": "order.created", "data": {"amount_minor": 12990, "currency": "BRL"}}


def test_accept_replay_conflict_and_restart(client, app, settings, headers):
    first = client.post("/v1/events", json=PAYLOAD, headers=headers)
    assert first.status_code == 202
    location = first.headers["location"]
    assert location == first.json()["status_url"]
    assert first.json()["status"] == "pending"
    replay = client.post(
        "/v1/events",
        headers=headers,
        content=json.dumps(
            {"data": {"currency": "BRL", "amount_minor": 12990}, "event_type": "order.created"}
        ),
    )
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert first.json()["event_id"] == replay.json()["event_id"]
    conflict = client.post(
        "/v1/events", headers=headers, json={**PAYLOAD, "data": {"amount_minor": 1}}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["request_id"] == conflict.headers["x-request-id"]
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 1
        assert session.scalar(select(func.count()).select_from(Delivery)) == 1
        assert json.loads(session.scalar(select(Event.outbound_body)))["data"] == PAYLOAD["data"]
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(location, headers=headers).json()["status"] == "pending"
    assert client.get(location + "/attempts", headers=headers).json() == []


def test_concurrent_identical_requests(client, app, headers):
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(lambda _: client.post("/v1/events", json=PAYLOAD, headers=headers), range(8))
        )
    assert sorted(r.status_code for r in responses) == [200] * 7 + [202]
    assert len({r.json()["event_id"] for r in responses}) == 1
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Delivery)) == 1


def test_event_and_delivery_rollback_together(app):
    # A failing delivery insert must roll back the event in the same transaction.
    event_id = uuid4()
    with pytest.raises(IntegrityError), app.state.sessions.begin() as session:
        session.add(
            Event(
                id=event_id,
                scope_id="operator",
                idempotency_key="rollback",
                fingerprint="x" * 64,
                event_type="order.created",
                outbound_body=b"{}",
            )
        )
        session.flush()
        session.add(Delivery(event_id=event_id, status="invalid"))
        session.flush()
    with app.state.sessions() as session:
        assert session.get(Event, event_id) is None


def test_authentication_health_and_missing(client, headers):
    for path in ("/v1/events/" + str(uuid4()), "/v1/events/" + str(uuid4()) + "/attempts"):
        assert client.get(path).status_code == 401
    assert (
        client.post("/v1/events", json=PAYLOAD, headers={"Idempotency-Key": "x"}).status_code == 401
    )
    assert client.get("/v1/events/" + str(uuid4()), headers=headers).status_code == 404
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").json()["components"]["securitySchemes"]["APIKeyHeader"]


def test_readiness_requires_current_migrations(client, app):
    with app.state.sessions.begin() as session:
        previous = session.scalar(text("SELECT version_num FROM alembic_version"))
        session.execute(text("UPDATE alembic_version SET version_num = 'stale'"))
    try:
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health/live").status_code == 200
    finally:
        with app.state.sessions.begin() as session:
            session.execute(
                text("UPDATE alembic_version SET version_num = :revision"), {"revision": previous}
            )


@pytest.mark.parametrize(
    "body",
    [
        {"event_type": "", "data": {}},
        {"event_type": "x" * 101, "data": {}},
        {"event_type": "x", "data": []},
        {**PAYLOAD, "destination_url": "http://example.com"},
        {"event_type": 1, "data": {}},
    ],
)
def test_invalid_payload(client, headers, body):
    assert client.post("/v1/events", headers=headers, json=body).status_code == 422


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999", '"\\ud800"'])
def test_unsupported_json_values(client, headers, value):
    response = client.post(
        "/v1/events", headers=headers, content='{"event_type":"x","data":{"value":' + value + "}}"
    )
    assert response.status_code == 422


@pytest.mark.parametrize("key", ["", "x" * 129])
def test_invalid_key(client, headers, key):
    assert (
        client.post(
            "/v1/events", headers={**headers, "Idempotency-Key": key}, json=PAYLOAD
        ).status_code
        == 422
    )


def test_body_limit(client, headers):
    assert (
        client.post("/v1/events", headers=headers, content=b"x" * (MAX_BODY + 1)).status_code == 413
    )


def test_chunked_body_limit(app):
    async def invoke():
        chunks = iter(
            [
                {"type": "http.request", "body": b"x" * 9000, "more_body": True},
                {"type": "http.request", "body": b"x" * 9000, "more_body": False},
            ]
        )
        sent = []

        async def receive():
            return next(chunks)

        async def send(message):
            sent.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "path": "/v1/events",
                "raw_path": b"/v1/events",
                "query_string": b"",
                "scheme": "http",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("test", 80),
            },
            receive,
            send,
        )
        assert sent[0]["status"] == 413

    asyncio.run(invoke())
