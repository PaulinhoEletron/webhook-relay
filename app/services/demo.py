import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.api.schemas import canonical
from app.db.models import Delivery, DemoRun, Event
from app.services.sandbox import admit


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_run(sessions, scenario, key, settings, *, now=None):
    run_id, event_id = uuid4(), uuid4()
    token = secrets.token_urlsafe(32)
    now = now or datetime.now(UTC)
    expires = now + timedelta(hours=1)
    body = canonical(
        {
            "id": str(event_id),
            "type": "order.created",
            "created_at": now.isoformat(),
            "data": {"order_id": "demo-order", "amount": 4200, "currency": "USD"},
        }
    )
    with sessions.begin() as session:
        admit(session, key, settings, now)
        session.add(
            Event(
                id=event_id,
                scope_id="demo",
                idempotency_key=str(run_id),
                fingerprint=hashlib.sha256(body).hexdigest(),
                event_type="order.created",
                outbound_body=body,
                created_at=now,
            )
        )
        session.flush()
        session.add(Delivery(event_id=event_id))
        session.add(
            DemoRun(
                id=run_id,
                client_key=key,
                event_id=event_id,
                token_hash=token_hash(token),
                scenario=scenario,
                expires_at=expires,
            )
        )
    return {
        "run_id": run_id,
        "event_id": event_id,
        "read_token": token,
        "expires_at": expires,
        "status_url": f"/demo/runs/{run_id}",
    }
