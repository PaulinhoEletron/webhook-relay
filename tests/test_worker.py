import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db.models import Delivery
from app.signing import signature
from app.worker.main import claim, finalize, run_once
from receiver.main import create_app
from tests.test_ingestion import PAYLOAD


def test_concurrent_claims(client, app, headers):
    client.post("/v1/events", headers=headers, json=PAYLOAD)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: claim(app.state.sessions, 60), range(2)))
    assert sum(job is not None for job in jobs) == 1
    job = next(job for job in jobs if job is not None)
    assert not finalize(app.state.sessions, (job[0], uuid4(), *job[2:]), 200, None, 1)
    assert finalize(app.state.sessions, job, 200, None, 1)


@pytest.mark.parametrize("response_status", [200, 400, 503, 302])
def test_real_http_delivery(client, app, settings, headers, response_status):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.headers, body))
            self.send_response(response_status)
            self.send_header("Location", "/do-not-follow")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        event = client.post("/v1/events", headers=headers, json=PAYLOAD).json()
        # Test-only override: production configuration rejects arbitrary HTTP destinations.
        settings = settings.model_copy(
            update={"destination_url": f"http://127.0.0.1:{server.server_port}/webhooks"}
        )

        async def deliver():
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as transport:
                assert await run_once(app.state.sessions, settings, transport)
                assert not await run_once(app.state.sessions, settings, transport)

        asyncio.run(deliver())
        state = client.get(event["status_url"], headers=headers).json()
        assert state["status"] == (
            "delivered"
            if response_status == 200
            else "retry_wait"
            if response_status == 503
            else "failed"
        )
        attempts = client.get(event["status_url"] + "/attempts", headers=headers).json()
        assert len(attempts) == 1
        assert attempts[0]["http_status"] == response_status
        assert len(received) == 1
        request_headers, body = received[0]
        assert json.loads(body)["id"] == event["event_id"]
        assert request_headers["X-Webhook-Signature"] == signature(
            settings.signing_secret.get_secret_value(), request_headers["X-Webhook-Timestamp"], body
        )
        with app.state.sessions() as session:
            assert session.get(Delivery, UUID(event["event_id"])).lease_token is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_receiver_signature(settings):
    event_id = str(uuid4())
    body = json.dumps({"id": event_id, "type": "order.created", "data": {}}).encode()
    timestamp = str(int(time.time()))
    headers = {
        "X-Webhook-Id": event_id,
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": signature(
            settings.signing_secret.get_secret_value(), timestamp, body
        ),
    }
    with TestClient(create_app(settings)) as receiver:
        assert receiver.post("/webhooks", headers=headers, content=body).status_code == 200
        assert receiver.post("/webhooks", headers=headers, content=body + b" ").status_code == 401
        expired = str(int(time.time()) - 600)
        headers.update(
            {
                "X-Webhook-Timestamp": expired,
                "X-Webhook-Signature": signature(
                    settings.signing_secret.get_secret_value(), expired, body
                ),
            }
        )
        assert receiver.post("/webhooks", headers=headers, content=body).status_code == 401
