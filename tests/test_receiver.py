import json
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from app.db.models import FulfillmentEffect
from app.signing import signature
from receiver.main import create_app


def signed(settings, event_id, body):
    timestamp = str(int(time.time()))
    return {
        "X-Webhook-Id": str(event_id),
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": signature(
            settings.signing_secret.get_secret_value(), timestamp, body
        ),
    }


def test_concurrent_duplicates_and_receiver_restart(app, settings):
    event_id = uuid4()
    body = json.dumps({"id": str(event_id), "type": "order.created", "data": {}}).encode()
    headers = signed(settings, event_id, body)
    with TestClient(create_app(settings)) as receiver:
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(
                pool.map(
                    lambda _: receiver.post("/webhooks", content=body, headers=headers), range(8)
                )
            )
        assert all(r.status_code == 200 for r in replies)
    with TestClient(create_app(settings)) as restarted:
        assert restarted.post("/webhooks", content=body, headers=headers).status_code == 200
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(FulfillmentEffect)) == 1
        assert session.get(FulfillmentEffect, event_id).outbound_body == body
    # Event ID owns the effect: a repeated receipt cannot overwrite it.
    changed = json.dumps({"id": str(event_id), "data": {"changed": True}}).encode()
    with TestClient(create_app(settings)) as receiver:
        assert (
            receiver.post(
                "/webhooks", content=changed, headers=signed(settings, event_id, changed)
            ).status_code
            == 200
        )
    with app.state.sessions() as session:
        assert session.get(FulfillmentEffect, event_id).outbound_body == body


def test_invalid_receipts_do_not_create_effect(app, settings):
    event_id = uuid4()
    with TestClient(create_app(settings)) as receiver:
        for body in (b"{}", b"[]", b"null", b"not-json", b'{"id":"invalid"}'):
            assert (
                receiver.post(
                    "/webhooks", content=body, headers=signed(settings, event_id, body)
                ).status_code
                == 422
            )
        body = json.dumps({"id": str(uuid4())}).encode()
        assert (
            receiver.post(
                "/webhooks", content=body, headers=signed(settings, event_id, body)
            ).status_code
            == 422
        )
        assert receiver.post("/webhooks", content=body).status_code == 401
        assert receiver.post("/webhooks", content=b"x" * (32 * 1024 + 1)).status_code == 413
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(FulfillmentEffect)) == 0


def test_effect_rollback_and_retry_after_database_failure(app, settings):
    event_id = uuid4()
    body = json.dumps({"id": str(event_id), "data": {}}).encode()
    receiver_app = create_app(settings)
    engine = receiver_app.state.sessions.kw["bind"]

    def fail_after_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO fulfillment_effects"):
            raise OperationalError("private SQL", {}, Exception("private database details"))

    event.listen(engine, "after_cursor_execute", fail_after_insert)
    with TestClient(receiver_app) as receiver:
        response = receiver.post(
            "/webhooks", content=body, headers=signed(settings, event_id, body)
        )
        assert response.status_code == 503
        assert response.json() == {"detail": "Receiver database unavailable"}
        with app.state.sessions() as session:
            assert session.get(FulfillmentEffect, event_id) is None
        event.remove(engine, "after_cursor_execute", fail_after_insert)
        assert (
            receiver.post(
                "/webhooks", content=body, headers=signed(settings, event_id, body)
            ).status_code
            == 200
        )
    with app.state.sessions() as session:
        assert session.get(FulfillmentEffect, event_id) is not None
