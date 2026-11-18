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
from app.metrics import QUOTA_REJECTIONS

LOCK_ID = 63006


def normalize_ip(value):
    """Canonical key material: IPv4 in full, IPv6 by /64, IPv4-mapped IPv6 as IPv4."""
    address = ipaddress.ip_address(value.strip())
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 64), strict=False))
    return str(address)


def client_key(request, settings):
    peer = request.client.host if request.client else "unknown"
    try:
        peer = normalize_ip(peer)
    except ValueError:
        pass
    if settings.trust_cf_connecting_ip:
        # Only valid when the app is reachable solely through Cloudflare's tunnel.
        values = request.headers.getlist("cf-connecting-ip")
        if len(values) == 1:
            try:
                peer = normalize_ip(values[0])
            except ValueError:
                pass
    # X-Forwarded-For and X-Real-IP never participate in client identity.
    return hmac.new(
        settings.signing_secret.get_secret_value().encode(), peer.encode(), hashlib.sha256
    ).hexdigest()


def limited(seconds, reason):
    QUOTA_REJECTIONS.labels(reason).inc()
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
    for query, limit, reason in (
        (recent.where(DemoRun.client_key == key), 5, "client_rate"),
        (recent, 30, "global_rate"),
    ):
        times = session.scalars(query.order_by(Event.created_at)).all()
        if len(times) >= limit:
            limited((times[-limit] + timedelta(minutes=1) - now).total_seconds(), reason)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily = session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.scope_id == "demo", Event.created_at >= day)
    )
    if daily >= 1000:
        limited((day + timedelta(days=1) - now).total_seconds(), "daily")
    active = session.scalar(
        select(func.count())
        .select_from(Delivery)
        .join(Event)
        .where(
            Event.scope_id == "demo", Delivery.status.in_(("pending", "processing", "retry_wait"))
        )
    )
    if active >= 100:
        limited(60, "active")
    size = session.scalar(text("SELECT pg_database_size(current_database())"))
    if size >= settings.demo_storage_limit_bytes:
        limited(60, "storage")


def poll(sessions, key, *, now=None):
    # Global cap also bounds row creation by clients with fabricated identities.
    now = now or datetime.now(UTC)
    window = now.replace(second=0, microsecond=0)
    with sessions.begin() as session:
        lock(session)
        session.execute(delete(PollBucket).where(PollBucket.window_start < window))
        for identity, limit, reason in (("global", 600, "poll_global"), (key, 60, "poll_client")):
            bucket = session.get(PollBucket, identity)
            if bucket is not None and bucket.count >= limit:
                limited(60 - now.second, reason)
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
