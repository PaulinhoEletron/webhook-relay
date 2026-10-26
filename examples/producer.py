"""Create one synthetic event and wait for actual worker delivery."""

import os
import time
from uuid import uuid4

import httpx

base_url = os.environ.get("RELAY_API_URL", "http://localhost:8000")
with httpx.Client(
    base_url=base_url, headers={"X-API-Key": os.environ["OPERATOR_API_KEY"]}, timeout=5
) as client:
    response = client.post(
        "/v1/events",
        headers={"Idempotency-Key": f"demo-{uuid4()}"},
        json={
            "event_type": "order.created",
            "data": {"order_id": "demo-order-001", "amount_minor": 12990, "currency": "BRL"},
        },
    )
    response.raise_for_status()
    event = response.json()
    print(f"Accepted {event['event_id']}", flush=True)
    for _ in range(30):
        response = client.get(event["status_url"])
        response.raise_for_status()
        state = response.json()
        if state["status"] in ("delivered", "failed"):
            attempts = client.get(event["status_url"] + "/attempts")
            attempts.raise_for_status()
            print({"status": state["status"], "attempts": attempts.json()})
            if state["status"] != "delivered":
                raise SystemExit("Delivery failed")
            break
        time.sleep(1)
    else:
        raise SystemExit("Delivery did not finish within 30 seconds")
