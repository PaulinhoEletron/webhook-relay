from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.requests import Request

from app.db.models import Attempt, Delivery, DemoRun, Event, FulfillmentEffect, PollBucket
from app.main import create_app
from app.services.demo import create_run
from app.services.sandbox import cleanup, client_key, poll
from tests.test_demo import create


def seed(sessions, count, now, status="delivered", expires=None):
    ids = [uuid4() for _ in range(count)]
    with sessions.begin() as session:
        session.add_all(
            [
                Event(
                    id=id,
                    scope_id="demo",
                    idempotency_key=str(id),
                    fingerprint="x",
                    event_type="order.created",
                    outbound_body=b"{}",
                    created_at=now,
                )
                for id in ids
            ]
        )
        session.flush()
        session.add_all([Delivery(event_id=id, status=status) for id in ids])
        session.add_all(
            [
                DemoRun(
                    id=uuid4(),
                    event_id=id,
                    token_hash="x",
                    client_key="seed",
                    scenario="success",
                    expires_at=expires or now + timedelta(hours=1),
                )
                for id in ids
            ]
        )
    return ids


def test_concurrent_client_limit_and_untrusted_forwarding(app, client):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda n: client.post(
                    "/demo/runs",
                    json={"scenario": "success"},
                    headers={"X-Forwarded-For": f"192.0.2.{n}"},
                ),
                range(8),
            )
        )
    assert sorted(r.status_code for r in results) == [202] * 5 + [429] * 3
    for result in results:
        if result.status_code == 429:
            assert 1 <= int(result.headers["retry-after"]) <= 60
            assert result.json()["error"]["code"] == "quota_exceeded"
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 5


def test_concurrent_global_limit_and_sliding_window(app, settings):
    now = datetime.now(UTC)

    def attempt(n):
        try:
            create_run(app.state.sessions, "success", str(n), settings, now=now)
            return 202
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(40)))
    assert results.count(202) == 30 and results.count(429) == 10
    with pytest.raises(HTTPException) as exc:
        create_run(app.state.sessions, "success", "new", settings, now=now + timedelta(seconds=59))
    assert exc.value.headers["Retry-After"] == "1"
    create_run(app.state.sessions, "success", "new", settings, now=now + timedelta(seconds=60))


def test_daily_limit_resets_at_utc_midnight(app, settings):
    now = datetime(2026, 10, 20, 23, 59, 50, tzinfo=UTC)
    seed(app.state.sessions, 1000, now - timedelta(hours=1))
    with pytest.raises(HTTPException) as exc:
        create_run(app.state.sessions, "success", "new", settings, now=now)
    assert exc.value.headers["Retry-After"] == "10"
    create_run(app.state.sessions, "success", "new", settings, now=now + timedelta(seconds=10))


def test_concurrent_active_cap(app, settings):
    now = datetime.now(UTC)
    seed(app.state.sessions, 99, now - timedelta(minutes=2), "retry_wait")

    def attempt(n):
        try:
            create_run(app.state.sessions, "success", str(n), settings, now=now)
            return 202
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(attempt, range(4))) == [202, 429, 429, 429]
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Delivery)) == 100


def test_storage_quota_and_rejected_creations_are_atomic(app, settings):
    with TestClient(
        create_app(settings.model_copy(update={"demo_storage_limit_bytes": 1}))
    ) as client:
        response = client.post("/demo/runs", json={"scenario": "success"})
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 0


def test_poll_limit_counts_invalid_tokens_and_resets(app, client):
    run = create(client)
    for _ in range(60):
        assert client.get(run["status_url"]).status_code == 401
    response = client.get(run["status_url"])
    assert response.status_code == 429
    assert 1 <= int(response.headers["retry-after"]) <= 60
    with app.state.sessions.begin() as session:
        for bucket in session.scalars(select(PollBucket)):
            bucket.window_start -= timedelta(minutes=1)
    assert client.get(run["status_url"]).status_code == 401


def test_poll_global_cap_bounds_cardinality(app):
    now = datetime.now(UTC)
    with app.state.sessions.begin() as session:
        session.add(
            PollBucket(key="global", count=599, window_start=now.replace(second=0, microsecond=0))
        )
    poll(app.state.sessions, "last", now=now)
    with pytest.raises(HTTPException):
        poll(app.state.sessions, "new", now=now)
    with app.state.sessions() as session:
        assert session.get(PollBucket, "new") is None
    poll(app.state.sessions, "new", now=now + timedelta(minutes=1))
    with app.state.sessions() as session:
        assert session.get(PollBucket, "last") is None


def _request(peer, headers):
    return Request(
        {
            "type": "http",
            "client": (peer, 1000),
            "headers": [(k.encode(), v.encode()) for k, v in headers],
        }
    )


def test_forwarded_headers_never_change_identity(settings):
    direct = client_key(_request("192.0.2.1", []), settings)
    spoofed = [("x-forwarded-for", "192.0.2.9"), ("x-real-ip", "192.0.2.8")]
    assert client_key(_request("192.0.2.1", spoofed), settings) == direct
    # CF-Connecting-IP is ignored unless explicitly trusted.
    assert (
        client_key(_request("192.0.2.1", [("cf-connecting-ip", "192.0.2.7")]), settings) == direct
    )


def test_cloudflare_identity_is_explicit_and_unambiguous(settings):
    trusted = settings.model_copy(update={"trust_cf_connecting_ip": True})
    direct = client_key(_request("172.18.0.5", []), trusted)
    one = client_key(_request("172.18.0.5", [("cf-connecting-ip", "192.0.2.7")]), trusted)
    other = client_key(_request("172.18.0.5", [("cf-connecting-ip", "192.0.2.8")]), trusted)
    assert len({direct, one, other}) == 3
    assert one == client_key(_request("172.18.0.6", [("cf-connecting-ip", "192.0.2.7")]), trusted)
    for headers in (
        [("cf-connecting-ip", "unknown")],
        [("cf-connecting-ip", "")],
        [("cf-connecting-ip", "192.0.2.7"), ("cf-connecting-ip", "192.0.2.8")],
        [("cf-connecting-ip", "192.0.2.7, 192.0.2.8")],
    ):
        assert client_key(_request("172.18.0.5", headers), trusted) == direct
        assert (
            client_key(_request("172.18.0.5", headers + [("x-forwarded-for", "1.1.1.1")]), trusted)
            == direct
        )


def test_ipv6_keyed_by_64_and_mapped_ipv4(settings):
    trusted = settings.model_copy(update={"trust_cf_connecting_ip": True})

    def key(value):
        return client_key(_request("172.18.0.5", [("cf-connecting-ip", value)]), trusted)

    assert key("2001:db8:1:2::1") == key("2001:db8:1:2:ffff::9")
    assert key("2001:db8:1:2::1") != key("2001:db8:1:3::1")
    assert key("::ffff:192.0.2.7") == key("192.0.2.7")
    assert key("192.0.2.7") != key("192.0.2.8")
    assert client_key(_request("2001:db8::1", []), settings) == client_key(
        _request("2001:db8::ffff", []), settings
    )


def test_cleanup_only_expired_terminal_demo_data(app, client, headers):
    now = datetime.now(UTC)
    old = now - timedelta(hours=26)
    expired = now - timedelta(hours=24)
    removed = seed(app.state.sessions, 1, old, expires=expired)[0]
    active = seed(app.state.sessions, 1, old, "processing", expires=expired)[0]
    recent = seed(app.state.sessions, 1, old, expires=expired + timedelta(seconds=1))[0]
    operator = UUID(
        client.post(
            "/v1/events", headers=headers, json={"event_type": "order.created", "data": {}}
        ).json()["event_id"]
    )
    with app.state.sessions.begin() as session:
        session.add(
            Attempt(event_id=removed, attempt_number=1, lease_token=uuid4(), outcome="delivered")
        )
        session.add_all(
            [FulfillmentEffect(event_id=id, outbound_body=b"{}") for id in (removed, operator)]
        )
    assert cleanup(app.state.sessions, now=now) == 1
    assert cleanup(app.state.sessions, now=now) == 0
    with app.state.sessions() as session:
        assert session.get(Event, removed) is None
        assert session.get(FulfillmentEffect, removed) is None
        assert session.scalar(select(Attempt).where(Attempt.event_id == removed)) is None
        assert session.scalar(select(DemoRun).where(DemoRun.event_id == removed)) is None
        assert all(session.get(Event, id) is not None for id in (active, recent, operator))
        assert session.get(FulfillmentEffect, operator) is not None


def test_public_inputs_and_logs_do_not_expose_secrets(client, settings, caplog):
    with caplog.at_level("INFO", logger="relay.api"):
        run = create(client)
        response = client.get(
            run["status_url"], headers={"Authorization": f"Bearer {run['read_token']}"}
        )
    assert run["read_token"] not in caplog.text
    assert settings.operator_api_key.get_secret_value() not in caplog.text
    assert set(response.json()) == {
        "run_id",
        "event_id",
        "scenario",
        "created_at",
        "expires_at",
        "status",
        "attempts_used",
        "delivered_at",
        "attempts",
    }
    for field in ("url", "headers", "payload", "data"):
        assert client.post("/demo/runs", json={"scenario": "success", field: {}}).status_code == 422
    assert client.post("/demo/runs", content=b"x" * 16385).status_code == 413


def test_running_worker_cleans_expired_runs(app, settings):
    import os
    import subprocess
    import sys
    import time

    old = datetime.now(UTC) - timedelta(hours=26)
    event_id = seed(app.state.sessions, 1, old)[0]
    process = subprocess.Popen(
        [sys.executable, "-m", "app.worker.main"],
        env={
            **os.environ,
            "DATABASE_URL": settings.database_url,
            "OPERATOR_API_KEY": settings.operator_api_key.get_secret_value(),
            "SIGNING_SECRET": settings.signing_secret.get_secret_value(),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with app.state.sessions() as session:
                if session.get(Event, event_id) is None:
                    break
            assert process.poll() is None
            time.sleep(0.1)
        else:
            pytest.fail("Worker did not clean expired terminal run")
    finally:
        process.terminate()
        _, stderr = process.communicate(timeout=12)
    assert process.returncode == 0, stderr.decode()
