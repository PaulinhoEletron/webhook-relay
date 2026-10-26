import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from app.db.models import Delivery, DemoRun, Event, FulfillmentEffect, WorkerHeartbeat
from app.main import create_app
from app.worker.main import run_once
from receiver.main import create_app as receiver_app
from tests.test_receiver import signed


def create(client, scenario="success"):
    response = client.post("/demo/runs", json={"scenario": scenario})
    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def read(client, run):
    return client.get(run["status_url"], headers={"Authorization": f"Bearer {run['read_token']}"})


def test_isolated_pipeline_histories(app, client, settings):
    scenarios = ["success", "temporary_failure", "permanent_failure", "temporary_failure"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(lambda scenario: create(client, scenario), scenarios))
    now = datetime.now(UTC)

    async def deliver():
        nonlocal now
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=receiver_app(settings))
        ) as http:
            for _ in range(12):
                await run_once(app.state.sessions, settings, http, clock=lambda: now)
                now += timedelta(seconds=5)

    asyncio.run(deliver())
    for run, scenario in zip(runs, scenarios, strict=True):
        result = read(client, run)
        assert result.status_code == 200
        history = result.json()
        expected = {
            "success": [200],
            "temporary_failure": [503, 503, 200],
            "permanent_failure": [400],
        }[scenario]
        assert [a["http_status"] for a in history["attempts"]] == expected
        assert history["status"] == ("failed" if scenario == "permanent_failure" else "delivered")
        assert "read_token" not in history and "token_hash" not in history
        with app.state.sessions() as session:
            stored = session.get(DemoRun, UUID(run["run_id"]))
            assert stored.receipts == len(expected)
            assert stored.token_hash != run["read_token"]
            assert (session.get(FulfillmentEffect, stored.event_id) is not None) == (
                expected[-1] == 200
            )


def test_token_isolation_expiry_and_operator_boundary(app, client, headers):
    first, second = create(client), create(client)
    assert client.get(first["status_url"]).status_code == 401
    assert (
        client.get(
            first["status_url"], headers={"Authorization": f"Bearer {second['read_token']}"}
        ).status_code
        == 401
    )
    assert client.get(first["status_url"] + "?token=" + first["read_token"]).status_code == 401
    assert client.get(f"/v1/events/{first['event_id']}", headers=headers).status_code == 404
    assert (
        client.get(f"/v1/events/{first['event_id']}/attempts", headers=headers).status_code == 404
    )
    with app.state.sessions.begin() as session:
        session.get(DemoRun, UUID(first["run_id"])).expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    assert read(client, first).status_code == 401
    assert read(client, second).status_code == 200


def test_validation_and_disabled_demo(client, settings):
    for payload in (
        {},
        {"scenario": "unknown"},
        {"scenario": "success", "url": "https://example.com"},
    ):
        assert client.post("/demo/runs", json=payload).status_code == 422
    with TestClient(create_app(settings.model_copy(update={"demo_mode": False}))) as disabled:
        assert disabled.post("/demo/runs", json={"scenario": "success"}).status_code == 404
        assert disabled.get(create(client)["status_url"]).status_code == 404


def test_creation_is_atomic(app, client):
    engine = app.state.sessions.kw["bind"]

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO demo_runs"):
            raise OperationalError("private", {}, Exception("private"))

    event.listen(engine, "after_cursor_execute", fail)
    try:
        assert client.post("/demo/runs", json={"scenario": "success"}).status_code == 503
    finally:
        event.remove(engine, "after_cursor_execute", fail)
    with app.state.sessions() as session:
        assert session.scalar(select(Event)) is None
        assert session.scalar(select(Delivery)) is None


def test_worker_freshness_independent_of_readiness(app, client, settings):
    assert client.get("/health/worker").json()["status"] == "offline"
    assert client.get("/health/ready").status_code == 200

    async def idle():
        async with httpx.AsyncClient() as http:
            assert not await run_once(app.state.sessions, settings, http)

    asyncio.run(idle())
    assert client.get("/health/worker").json()["status"] == "online"
    with app.state.sessions.begin() as session:
        session.get(WorkerHeartbeat, "delivery").last_seen_at -= timedelta(seconds=31)
    assert client.get("/health/worker").json()["status"] == "offline"
    assert client.get("/health/ready").status_code == 200


def test_receiver_counter_survives_restart_and_concurrent_receipts(app, client, settings):
    run = create(client, "temporary_failure")
    with app.state.sessions() as session:
        body = session.get(Event, UUID(run["event_id"])).outbound_body
    headers = signed(settings, run["event_id"], body)
    with TestClient(receiver_app(settings)) as receiver:
        assert receiver.post("/webhooks", content=body, headers=headers).status_code == 503
    with TestClient(receiver_app(settings)) as receiver:
        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = list(
                pool.map(
                    lambda _: receiver.post("/webhooks", content=body, headers=headers).status_code,
                    range(4),
                )
            )
        assert sorted(codes) == [200, 200, 200, 503]
    with app.state.sessions() as session:
        assert session.get(DemoRun, UUID(run["run_id"])).receipts == 5
        assert session.get(FulfillmentEffect, UUID(run["event_id"])) is not None
