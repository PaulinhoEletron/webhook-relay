"""PostgreSQL-serialized sandbox admission, bounded polling, and retention."""

import hashlib
import hmac
import ipaddress
import math
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Delivery, DemoRun, Event, FulfillmentEffect, PollBucket

LOCK_ID = 63006


def client_key(request, settings):
    peer = request.client.host if request.client else "unknown"
    try:
        trusted = any(
            ipaddress.ip_address(peer) in ipaddress.ip_network(network)
            for network in settings.trusted_proxy_cidrs
        )
    except ValueError:
        trusted = False
    if trusted:
        forwarded = request.headers.getlist("x-forwarded-for")
        try:
            if len(forwarded) != 1:
                raise ValueError
            peer = str(ipaddress.ip_address(forwarded[0].strip()))
        except ValueError:
            raise HTTPException(400, "invalid_request") from None
    # Untrusted forwarded headers never participate in client identity.
    return hmac.new(
        settings.signing_secret.get_secret_value().encode(), peer.encode(), hashlib.sha256
    ).hexdigest()


def limited(seconds):
    raise HTTPException(
        429, "quota_exceeded", headers={"Retry-After": str(max(1, math.ceil(seconds)))}
    )


def lock(session):
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_ID})


def admit(session, key, settings, now):
    lock(session)
    minute = now - timedelta(minutes=1)
    recent = (
        select(Event.created_at)
        .join(DemoRun, DemoRun.event_id == Event.id)
        .where(Event.created_at > minute)
    )
    for query, limit in ((recent.where(DemoRun.client_key == key), 5), (recent, 30)):
        times = session.scalars(query.order_by(Event.created_at)).all()
        if len(times) >= limit:
            limited((times[-limit] + timedelta(minutes=1) - now).total_seconds())
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily = session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.scope_id == "demo", Event.created_at >= day)
    )
    if daily >= 1000:
        limited((day + timedelta(days=1) - now).total_seconds())
    active = session.scalar(
        select(func.count())
        .select_from(Delivery)
        .join(Event)
        .where(
            Event.scope_id == "demo", Delivery.status.in_(("pending", "processing", "retry_wait"))
        )
    )
    if active >= 100:
        limited(60)
    size = session.scalar(text("SELECT pg_database_size(current_database())"))
    if size >= settings.demo_storage_limit_bytes:
        limited(60)


def poll(sessions, key, *, now=None):
    # Global cap also bounds row creation by clients with fabricated identities.
    now = now or datetime.now(UTC)
    window = now.replace(second=0, microsecond=0)
    with sessions.begin() as session:
        lock(session)
        session.execute(delete(PollBucket).where(PollBucket.window_start < window))
        for identity, limit in (("global", 600), (key, 60)):
            bucket = session.get(PollBucket, identity)
            if bucket is not None and bucket.count >= limit:
                limited(60 - now.second)
        for identity in ("global", key):
            session.execute(
                insert(PollBucket)
                .values(key=identity, window_start=window, count=1)
                .on_conflict_do_update(
                    index_elements=[PollBucket.key], set_={"count": PollBucket.count + 1}
                )
            )


def cleanup(sessions, *, now=None):
    now = now or datetime.now(UTC)
    with sessions.begin() as session:
        lock(session)
        ids = session.scalars(
            select(Event.id)
            .join(DemoRun)
            .join(Delivery)
            .where(
                Event.scope_id == "demo",
                DemoRun.expires_at <= now - timedelta(hours=24),
                Delivery.status.in_(("delivered", "failed")),
            )
            .limit(100)
        ).all()
        # Only terminal synthetic demo effects are removed; operator effects remain durable.
        if ids:
            session.execute(delete(FulfillmentEffect).where(FulfillmentEffect.event_id.in_(ids)))
            session.execute(delete(Event).where(Event.id.in_(ids)))
        session.execute(
            delete(PollBucket).where(PollBucket.window_start < now - timedelta(minutes=1))
        )
        return len(ids)
