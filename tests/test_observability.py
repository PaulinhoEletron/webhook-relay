import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from prometheus_client import REGISTRY

from app.logging import JsonFormatter, configure_logging
from app.worker.main import run_once
from tests.test_ingestion import PAYLOAD


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0


def formatted(caplog):
    return [JsonFormatter().format(record) for record in caplog.records]


def test_access_log_is_json_without_secrets(client, settings, headers, caplog):
    secrets = (
        settings.operator_api_key.get_secret_value(),
        settings.signing_secret.get_secret_value(),
    )
    with caplog.at_level(logging.INFO):
        client.post("/v1/events", headers=headers, json=PAYLOAD)
        client.get("/health/live")
        client.get("/no/such/path/12345")
    lines = [json.loads(line) for line in formatted(caplog)]
    access = [line for line in lines if line["logger"] == "relay.api"]
    assert len(access) == 3
    for line in lines:
        assert {"ts", "level", "logger", "message"} <= set(line)
        assert line["ts"].endswith("Z")
    post, live, unmatched = access
    assert (post["method"], post["route"], post["status"]) == ("POST", "/v1/events", 202)
    assert live["route"] == "/health/live"
    assert unmatched["route"] == "<unmatched>"
    assert all(len(line["client"]) == 16 and "testclient" not in line["client"] for line in access)
    assert all("duration_ms" in line and line["request_id"] for line in access)
    text = "\n".join(formatted(caplog))
    for secret in secrets:
        assert secret not in text


def test_uvicorn_access_log_stays_off():
    # uvicorn only writes its access line when the logger has a handler in its chain.
    configure_logging()
    assert not logging.getLogger("uvicorn.access").hasHandlers()
    assert logging.getLogger("uvicorn.error").hasHandlers()


def test_metrics_not_served_on_public_port(client):
    assert client.get("/metrics").status_code == 404


def test_counters_move_after_one_delivery(client, app, settings, headers, caplog):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    before = {
        "events": sample("events_accepted_total"),
        "attempts": sample("delivery_attempts_total", outcome="delivered"),
        "terminal": sample("deliveries_terminal_total", status="delivered"),
        "http": sample("http_requests_total", route="/v1/events", status="202"),
        "duration": sample("delivery_attempt_duration_seconds_count"),
    }
    try:
        client.post("/v1/events", headers=headers, json=PAYLOAD)
        # Test-only override: production configuration rejects arbitrary HTTP destinations.
        settings = settings.model_copy(
            update={"destination_url": f"http://127.0.0.1:{server.server_port}/webhooks"}
        )

        async def deliver():
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as transport:
                assert await run_once(app.state.sessions, settings, transport)

        with caplog.at_level(logging.INFO, logger="relay.worker"):
            asyncio.run(deliver())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert sample("events_accepted_total") == before["events"] + 1
    assert sample("delivery_attempts_total", outcome="delivered") == before["attempts"] + 1
    assert sample("deliveries_terminal_total", status="delivered") == before["terminal"] + 1
    assert sample("http_requests_total", route="/v1/events", status="202") == before["http"] + 1
    assert sample("delivery_attempt_duration_seconds_count") == before["duration"] + 1
    assert sample("worker_inflight") == 0
    line = json.loads(formatted(caplog)[-1])
    assert line["outcome"] == "delivered" and line["http_status"] == 200 and line["attempt"] == 1
    assert {"event_id", "duration_ms"} <= set(line)
