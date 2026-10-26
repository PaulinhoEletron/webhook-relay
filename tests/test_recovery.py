import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import Attempt, Delivery, FulfillmentEffect
from app.worker.main import claim, finalize, recover_expired, run_once
from receiver.main import create_app
from tests.test_ingestion import PAYLOAD


def worker_env(settings):
    return {
        **os.environ,
        "DATABASE_URL": settings.database_url,
        "OPERATOR_API_KEY": settings.operator_api_key.get_secret_value(),
        "SIGNING_SECRET": settings.signing_secret.get_secret_value(),
        "RETRY_PROFILE": "production",
    }


def test_crashed_process_after_claim_and_stale_finalization(client, app, settings, headers):
    event = client.post("/v1/events", headers=headers, json=PAYLOAD).json()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
from app.config import Settings
from app.db.session import database
from app.worker.main import claim
engine, sessions = database(Settings().database_url)
assert claim(sessions, 60) is not None
os._exit(23)
""",
        ],
        env=worker_env(settings),
        timeout=15,
        capture_output=True,
    )
    assert result.returncode == 23, result.stderr.decode()
    with app.state.sessions() as session:
        row = session.get(Delivery, UUID(event["event_id"]))
        expires = row.lease_expires_at
        old_job = (row.event_id, row.lease_token, 1, b"")
    assert recover_expired(app.state.sessions, now=expires - timedelta(microseconds=1)) == 0
    assert not finalize(app.state.sessions, old_job, 200, None, 1, now=expires)
    assert recover_expired(app.state.sessions, now=expires, jitter=lambda: 0) == 1
    assert recover_expired(app.state.sessions, now=expires, jitter=lambda: 0) == 0
    assert claim(app.state.sessions, 60, now=expires + timedelta(seconds=4)) is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(
            pool.map(
                lambda _: claim(app.state.sessions, 60, now=expires + timedelta(seconds=5)),
                range(2),
            )
        )
    assert sum(job is not None for job in jobs) == 1
    new_job = next(job for job in jobs if job)
    assert new_job[2] == 2 and new_job[1] != old_job[1]
    assert not finalize(
        app.state.sessions, old_job, 200, None, 1, now=expires + timedelta(seconds=6)
    )
    assert finalize(app.state.sessions, new_job, 200, None, 1, now=expires + timedelta(seconds=6))
    history = client.get(event["status_url"] + "/attempts", headers=headers).json()
    assert [a["outcome"] for a in history] == ["unknown", "delivered"]
    assert history[0]["http_status"] is None and history[0]["duration_ms"] is None
    assert client.get(event["status_url"], headers=headers).json()["status"] == "delivered"


def test_five_crashed_attempts_exhaust_budget(client, app, headers):
    event = client.post("/v1/events", headers=headers, json=PAYLOAD).json()
    now = datetime.now(UTC)
    for number, delay in enumerate([5, 15, 45, 120, 0], 1):
        job = claim(app.state.sessions, 60, now=now)
        assert job and job[2] == number
        now += timedelta(seconds=60)
        with ThreadPoolExecutor(max_workers=2) as pool:
            recovered = list(
                pool.map(
                    lambda _: recover_expired(app.state.sessions, now=now, jitter=lambda: 0),
                    range(2),
                )
            )
        assert sum(recovered) == 1
        now += timedelta(seconds=delay)
    assert claim(app.state.sessions, 60, now=now + timedelta(days=1)) is None
    state = client.get(event["status_url"], headers=headers).json()
    assert state["status"] == "failed" and state["attempts_used"] == 5
    history = client.get(event["status_url"] + "/attempts", headers=headers).json()
    assert len(history) == 5 and all(a["outcome"] == "unknown" for a in history)


def test_recovery_and_finalize_race(client, app, headers):
    client.post("/v1/events", headers=headers, json=PAYLOAD)
    now = datetime.now(UTC)
    job = claim(app.state.sessions, 60, now=now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        done = pool.submit(
            finalize, app.state.sessions, job, 200, None, 1, now=now + timedelta(seconds=59)
        )
        recovery = pool.submit(recover_expired, app.state.sessions, now=now + timedelta(seconds=60))
        assert int(done.result()) + recovery.result() == 1
    with app.state.sessions() as session:
        row = session.get(Delivery, job[0])
        attempt = session.scalar(select(Attempt).where(Attempt.event_id == job[0]))
        assert (row.status, attempt.outcome) in {
            ("delivered", "delivered"),
            ("retry_wait", "unknown"),
        }


@pytest.mark.parametrize("lose_response", [False, True])
def test_receiver_success_then_worker_crash_retries_once(
    client, app, settings, headers, lose_response
):
    event = client.post("/v1/events", headers=headers, json=PAYLOAD).json()
    receipts = []
    with TestClient(create_app(settings)) as receiver:

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                response = receiver.post("/webhooks", content=body, headers=dict(self.headers))
                receipts.append(response.status_code)
                if lose_response and len(receipts) == 1:
                    # Commit the effect, then drop the socket before sending HTTP headers.
                    self.close_connection = True
                    return
                self.send_response(response.status_code)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import asyncio, os, httpx, sys
from app.config import Settings
from app.db.session import database
import app.worker.main as worker
settings = Settings().model_copy(update={"destination_url": sys.argv[1]})
engine, sessions = database(settings.database_url)
worker.finalize = lambda *args, **kwargs: os._exit(24)
async def execute():
    async with httpx.AsyncClient(trust_env=False) as transport:
        await worker.run_once(sessions, settings, transport)
asyncio.run(execute())
""",
                    f"http://127.0.0.1:{server.server_port}/webhooks",
                ],
                env=worker_env(settings),
                timeout=15,
                capture_output=True,
            )
            assert result.returncode == 24, result.stderr.decode()
            assert receipts == [200]
            with app.state.sessions.begin() as session:
                row = session.get(Delivery, UUID(event["event_id"]))
                expires = row.lease_expires_at
                assert session.scalar(select(func.count()).select_from(FulfillmentEffect)) == 1
            # Advance past the lease while signatures remain within the five-minute window.
            configured = settings.model_copy(
                update={"destination_url": f"http://127.0.0.1:{server.server_port}/webhooks"}
            )

            async def retry():
                async with httpx.AsyncClient(trust_env=False) as transport:
                    assert not await run_once(
                        app.state.sessions,
                        configured,
                        transport,
                        clock=lambda: expires,
                        jitter=lambda: 0,
                    )
                    assert await run_once(
                        app.state.sessions,
                        configured,
                        transport,
                        clock=lambda: expires + timedelta(seconds=5),
                    )

            asyncio.run(retry())
            assert receipts == [200, 200]
            with app.state.sessions() as session:
                assert session.scalar(select(func.count()).select_from(FulfillmentEffect)) == 1
            history = client.get(event["status_url"] + "/attempts", headers=headers).json()
            assert [a["outcome"] for a in history] == ["unknown", "delivered"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def test_sigterm_finishes_inflight_and_leaves_pending_job(client, app, settings, headers):
    first = client.post("/v1/events", headers=headers, json=PAYLOAD).json()
    second = client.post(
        "/v1/events", headers={**headers, "Idempotency-Key": "shutdown-pending"}, json=PAYLOAD
    ).json()
    started, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            started.set()
            release.wait(5)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import asyncio, sys
import app.worker.main as worker
settings = worker.Settings().model_copy(update={"destination_url": sys.argv[1]})
worker.Settings = lambda: settings
asyncio.run(worker.main())
""",
            f"http://127.0.0.1:{server.server_port}/webhooks",
        ],
        env=worker_env(settings),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Process import/connection startup is not the in-flight request deadline.
        # Prepare both jobs beforehand so no database call delays SIGTERM here.
        assert started.wait(30)
        process.send_signal(signal.SIGTERM)
        time.sleep(0.1)
        release.set()
        _, stderr = process.communicate(timeout=12)
        assert process.returncode == 0, stderr.decode()
        assert client.get(first["status_url"], headers=headers).json()["status"] == "delivered"
        assert client.get(second["status_url"], headers=headers).json()["status"] == "pending"
    finally:
        release.set()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join()
