import asyncio
import threading
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

import httpx
import pytest

from app.db.models import Delivery
from app.signing import signature
from app.worker.main import run_once
from app.worker.retry import retry_after_seconds, retry_delay, retryable
from tests.test_ingestion import PAYLOAD

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


@pytest.mark.parametrize("status", [408, 429, 500, 503, 599])
def test_retryable_http_status(status):
    assert retryable(status, "http_error")


@pytest.mark.parametrize("status", [200, 204, 301, 302, 307, 400, 401, 404, 422, 600])
def test_non_retryable_http_status(status):
    assert not retryable(status, "http_error")


@pytest.mark.parametrize("error", ["connection_error", "timeout"])
def test_retryable_transport_error(error):
    assert retryable(None, error)


@pytest.mark.parametrize("attempt,base", [(1, 5), (2, 15), (3, 45), (4, 120)])
def test_production_delays_and_jitter_bounds(attempt, base):
    for jitter in (0, 0.5, 1):
        assert retry_delay(attempt, "production", 503, None, NOW, jitter) == pytest.approx(
            base * (1 + 0.2 * jitter)
        )


@pytest.mark.parametrize("attempt,delay", [(1, 2), (2, 4), (3, 8), (4, 12)])
def test_demo_delays_have_no_jitter(attempt, delay):
    assert retry_delay(attempt, "demo", 503, None, NOW, 1) == delay


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("nonsense", None),
        ("-1", None),
        ("1.5", None),
        ("１２", None),
        ("0", 0),
        (" 30 ", 30),
        ("000030", 30),
        ("999", 900),
        ("9" * 5000, 900),
        (format_datetime(NOW + timedelta(seconds=60), usegmt=True), 60),
        (format_datetime(NOW - timedelta(seconds=60), usegmt=True), 0),
        (format_datetime(NOW + timedelta(hours=1), usegmt=True), 900),
    ],
)
def test_retry_after_parsing(value, expected):
    assert retry_after_seconds(value, NOW) == expected


@pytest.mark.parametrize("status", [429, 503])
def test_server_delay_uses_greater_value_and_caps(status):
    assert retry_delay(2, "production", status, "1", NOW, 0) == 15
    assert retry_delay(2, "production", status, "90", NOW, 0) == 90
    assert retry_delay(2, "production", status, "99999", NOW, 0) == 900
    assert retry_delay(2, "production", status, "bad", NOW, 0) == 15


def test_other_status_does_not_honor_retry_after():
    assert retry_delay(1, "production", 500, "90", NOW, 0) == 5


@pytest.mark.parametrize("attempt", [0, 5, 6])
def test_no_delay_for_invalid_or_exhausted_budget(attempt):
    with pytest.raises(ValueError):
        retry_delay(attempt, "production", 503, None, NOW, 0)


@pytest.mark.parametrize(
    "responses,profile,server_delay,expected_delays,terminal",
    [
        ([503, 503, 200], "production", None, [5, 15], "delivered"),
        ([503, 503, 200], "demo", None, [2, 4], "delivered"),
        ([503] * 5, "production", None, [5, 15, 45, 120], "failed"),
        ([400], "production", None, [], "failed"),
        ([302], "production", None, [], "failed"),
        ([408, 200], "production", None, [5], "delivered"),
        ([429, 200], "production", "30", [30], "delivered"),
        ([503, 200], "production", "99999", [900], "delivered"),
        ([503, 200], "production", format_datetime(NOW + timedelta(seconds=60)), [60], "delivered"),
    ],
)
def test_real_http_retry_lifecycle(
    client, app, settings, headers, responses, profile, server_delay, expected_delays, terminal
):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.headers, body))
            self.send_response(responses[len(received) - 1])
            self.send_header("Location", "/must-not-follow")
            if server_delay:
                self.send_header("Retry-After", server_delay)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        event = client.post("/v1/events", headers=headers, json=PAYLOAD).json()
        event_id = UUID(event["event_id"])
        current = NOW
        with app.state.sessions.begin() as session:
            session.get(Delivery, event_id).next_attempt_at = current
        configured = settings.model_copy(
            update={
                "destination_url": f"http://127.0.0.1:{server.server_port}/webhooks",
                "retry_profile": profile,
            }
        )

        async def deliver():
            nonlocal current
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as transport:
                for index, response_status in enumerate(responses):
                    assert await run_once(
                        app.state.sessions,
                        configured,
                        transport,
                        clock=lambda: current,
                        jitter=lambda: 0,
                    )
                    state = client.get(event["status_url"], headers=headers).json()
                    assert state["attempts_used"] == index + 1
                    history = client.get(event["status_url"] + "/attempts", headers=headers).json()
                    assert [a["attempt_number"] for a in history] == list(range(1, index + 2))
                    assert [a["http_status"] for a in history] == responses[: index + 1]
                    assert history[-1]["outcome"] == (
                        "delivered" if response_status == 200 else "failed"
                    )
                    assert history[-1]["ended_at"] is not None
                    if index < len(expected_delays):
                        assert state["status"] == "retry_wait"
                        with app.state.sessions() as session:
                            row = session.get(Delivery, event_id)
                            due = row.next_attempt_at
                            assert due == current + timedelta(seconds=expected_delays[index])
                            assert row.lease_token is None and row.lease_expires_at is None
                        current = due - timedelta(microseconds=1)
                        assert not await run_once(
                            app.state.sessions, configured, transport, clock=lambda: current
                        )
                        current = due
                    else:
                        assert state["status"] == terminal
                current += timedelta(days=1)
                assert not await run_once(
                    app.state.sessions, configured, transport, clock=lambda: current
                )

        asyncio.run(deliver())
        assert len(received) == len(responses)
        assert len({body for _, body in received}) == 1
        for number, (sent_headers, body) in enumerate(received, 1):
            assert sent_headers["X-Webhook-Id"] == str(event_id)
            assert sent_headers["X-Webhook-Attempt"] == str(number)
            assert sent_headers["X-Webhook-Signature"] == signature(
                settings.signing_secret.get_secret_value(),
                sent_headers["X-Webhook-Timestamp"],
                body,
            )
        if len(received) > 1:
            assert received[0][0]["X-Webhook-Signature"] != received[1][0]["X-Webhook-Signature"]
        replay = client.post("/v1/events", headers=headers, json=PAYLOAD)
        assert replay.status_code == 200 and replay.json()["event_id"] == str(event_id)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("failure", ["connection", "phase_timeout", "deadline"])
def test_transport_failure_schedules_retry(client, app, settings, headers, failure):
    event = client.post("/v1/events", headers=headers, json=PAYLOAD).json()
    # Scheduling assertions must not depend on how quickly the host runs SQL.
    # The transport deadline below still uses the real asyncio clock.
    now = datetime.now(UTC)
    calls = []

    async def handler(request):
        calls.append(request)
        if failure == "connection":
            raise httpx.ConnectError("private downstream detail", request=request)
        if failure == "phase_timeout":
            raise httpx.ReadTimeout("private downstream detail", request=request)
        await asyncio.sleep(1)
        return httpx.Response(200)

    async def deliver():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            assert await run_once(
                app.state.sessions,
                settings.model_copy(update={"request_deadline": 0.01}),
                transport,
                clock=lambda: now,
                jitter=lambda: 0,
            )
            assert not await run_once(app.state.sessions, settings, transport, clock=lambda: now)

    asyncio.run(deliver())
    state = client.get(event["status_url"], headers=headers).json()
    assert state["status"] == "retry_wait"
    with app.state.sessions() as session:
        delivery = session.get(Delivery, UUID(event["event_id"]))
        assert delivery.next_attempt_at == now + timedelta(seconds=5)
    assert state["last_error_code"] == (
        "connection_error" if failure == "connection" else "timeout"
    )
    history = client.get(event["status_url"] + "/attempts", headers=headers).json()
    assert len(calls) == len(history) == 1
    assert history[0]["http_status"] is None
    assert "private downstream detail" not in str(state) + str(history)
