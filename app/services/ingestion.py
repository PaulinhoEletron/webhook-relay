import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.api.schemas import EventInput, canonical
from app.db.models import Delivery, Event


class IdempotencyConflict(Exception):
    pass


def ingest(sessions, payload: EventInput, key: str):
    fingerprint = hashlib.sha256(canonical(payload.model_dump())).hexdigest()
    event_id, now = uuid4(), datetime.now(UTC)
    body = canonical(
        {
            "id": str(event_id),
            "type": payload.event_type,
            "created_at": now.isoformat(),
            "data": payload.data,
        }
    )
    with sessions.begin() as session:
        inserted = session.scalar(
            insert(Event)
            .values(
                id=event_id,
                scope_id="operator",
                idempotency_key=key,
                fingerprint=fingerprint,
                event_type=payload.event_type,
                outbound_body=body,
                created_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_event_key")
            .returning(Event.id)
        )
        if inserted:
            session.add(Delivery(event_id=event_id))
            session.flush()
        else:
            # At READ COMMITTED this statement sees the winner after ON CONFLICT waits.
            event_id = session.scalar(
                select(Event.id).where(Event.scope_id == "operator", Event.idempotency_key == key)
            )
        event = session.get(Event, event_id)
        if event.fingerprint != fingerprint:
            raise IdempotencyConflict
        delivery = session.get(Delivery, event_id)
        result = {
            "event_id": event.id,
            "status": delivery.status,
            "status_url": f"/v1/events/{event.id}",
            "created_at": event.created_at,
        }
    # The transaction has committed before the API can acknowledge acceptance.
    return result, inserted is None
