"""Exercise the running Compose stack; optionally recheck an event after restart."""

import argparse
import os
import time
from uuid import UUID, uuid4

import httpx

from app.config import Settings
from app.db.models import FulfillmentEffect
from app.db.session import database
from app.signing import signature

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--event-id", help="Previously delivered event to verify after restart")
args = parser.parse_args()

with httpx.Client(
    base_url=os.environ.get("RELAY_API_URL", "http://localhost:8000"),
    headers={"X-API-Key": os.environ["OPERATOR_API_KEY"]},
    timeout=5,
) as client:
    for _ in range(30):
        try:
            if client.get("/health/ready").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    else:
        raise SystemExit("Readiness failed")
    for path in ("/health/live", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 200, path
    assert client.get("/openapi.json").json()["components"]["securitySchemes"]["APIKeyHeader"]

    if args.event_id:
        event_id = args.event_id
    else:
        payload = {"event_type": "order.created", "data": {"order_id": "regression-synthetic"}}
        headers = {"Idempotency-Key": f"regression-{uuid4()}"}
        accepted = client.post("/v1/events", headers=headers, json=payload)
        assert accepted.status_code == 202, accepted.status_code
        event_id = accepted.json()["event_id"]
        assert accepted.headers["Location"] == f"/v1/events/{event_id}"
        replay = client.post("/v1/events", headers=headers, json=payload)
        assert replay.status_code == 200
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json()["event_id"] == event_id
        assert (
            client.post("/v1/events", headers=headers, json={**payload, "data": {}}).status_code
            == 409
        )
        assert client.post("/v1/events", headers=headers, json={"data": {}}).status_code == 422
        assert client.post("/v1/events", headers=headers, content=b"x" * 16385).status_code == 413

    path = f"/v1/events/{event_id}"
    for suffix in ("", "/attempts"):
        assert client.get(path + suffix, headers={"X-API-Key": "invalid"}).status_code == 401
    assert client.get(f"/v1/events/{uuid4()}").status_code == 404
    for _ in range(30):
        state = client.get(path)
        state.raise_for_status()
        if state.json()["status"] in ("delivered", "failed"):
            break
        time.sleep(1)
    assert state.json()["status"] == "delivered", state.json()["status"]
    attempts = client.get(path + "/attempts")
    attempts.raise_for_status()
    history = attempts.json()
    assert len(history) == 1 and history[0]["http_status"] == 200, history
    assert history[0]["outcome"] == "delivered"
    print(f"PASS: live feature smoke checks; event_id={event_id}; one delivered HTTP 200 attempt")

# The receiver's durable business effect must survive restart, too. Replay the
# signed receipt over real HTTP and ensure it does not replace the original effect.
settings = Settings()
engine, sessions = database(settings.database_url)
try:
    with sessions() as session:
        effect = session.get(FulfillmentEffect, UUID(event_id))
        assert effect is not None, "Missing receiver fulfillment effect"
        body, created_at = effect.outbound_body, effect.created_at
    timestamp = str(int(time.time()))
    response = httpx.post(
        settings.destination_url,
        content=body,
        headers={
            "X-Webhook-Id": event_id,
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature": signature(
                settings.signing_secret.get_secret_value(), timestamp, body
            ),
        },
        timeout=5,
        trust_env=False,
    )
    assert response.status_code == 200
    with sessions() as session:
        effect = session.get(FulfillmentEffect, UUID(event_id))
        assert effect.created_at == created_at and effect.outbound_body == body
    print("PASS: durable effect exists; duplicate receipt returns 200 without repeating effect")
finally:
    engine.dispose()
