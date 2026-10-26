import asyncio
import json
import logging
import random
import signal
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.models import Attempt, Delivery, Event, WorkerHeartbeat
from app.db.session import database
from app.services.sandbox import cleanup
from app.signing import signature
from app.worker.retry import MAX_ATTEMPTS, retry_delay, retryable

logger = logging.getLogger("relay.worker")


def utc_now():
    return datetime.now(UTC)


def claim(sessions, lease_duration, *, now=None):
    now = now or utc_now()
    with sessions.begin() as session:
        delivery = session.scalar(
            select(Delivery)
            .where(
                Delivery.status.in_(("pending", "retry_wait")),
                Delivery.next_attempt_at <= now,
                Delivery.attempts_used < MAX_ATTEMPTS,
            )
            .order_by(Delivery.next_attempt_at, Delivery.event_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if delivery is None:
            return None
        token = uuid4()
        delivery.status = "processing"
        delivery.attempts_used += 1
        delivery.lease_token = token
        delivery.lease_expires_at = now + timedelta(seconds=lease_duration)
        delivery.updated_at = now
        session.add(
            Attempt(
                event_id=delivery.event_id,
                attempt_number=delivery.attempts_used,
                lease_token=token,
                started_at=now,
            )
        )
        event = session.get(Event, delivery.event_id)
        return event.id, token, delivery.attempts_used, event.outbound_body


def recover_expired(sessions, *, now=None, retry_profile="production", jitter=random.random):
    """Recover a bounded batch under row locks shared with claim/finalization."""
    now = now or utc_now()
    with sessions.begin() as session:
        expired = session.scalars(
            select(Delivery)
            .where(Delivery.status == "processing", Delivery.lease_expires_at <= now)
            .order_by(Delivery.lease_expires_at, Delivery.event_id)
            .with_for_update(skip_locked=True)
            .limit(100)
        ).all()
        for delivery in expired:
            attempt = session.scalar(
                select(Attempt).where(
                    Attempt.event_id == delivery.event_id,
                    Attempt.lease_token == delivery.lease_token,
                )
            )
            attempt.outcome = "unknown"
            attempt.ended_at = now
            # No HTTP result or duration is known after a worker disappeared.
            delivery.status = "retry_wait" if delivery.attempts_used < MAX_ATTEMPTS else "failed"
            if delivery.status == "retry_wait":
                delivery.next_attempt_at = now + timedelta(
                    seconds=retry_delay(
                        delivery.attempts_used,
                        "demo"
                        if session.get(Event, delivery.event_id).scope_id == "demo"
                        else retry_profile,
                        None,
                        None,
                        now,
                        jitter() if retry_profile == "production" else 0,
                    )
                )
            delivery.last_error_code = "lease_expired"
            delivery.lease_token = None
            delivery.lease_expires_at = None
            delivery.updated_at = now
        return len(expired)


def finalize(
    sessions,
    job,
    status,
    error,
    duration,
    *,
    retry_profile="production",
    retry_after=None,
    now=None,
    jitter=random.random,
):
    event_id, token, _, _ = job
    now = now or utc_now()
    with sessions.begin() as session:
        delivery = session.scalar(
            select(Delivery)
            .where(
                Delivery.event_id == event_id,
                Delivery.status == "processing",
                Delivery.lease_token == token,
                Delivery.lease_expires_at > now,
            )
            .with_for_update()
        )
        if delivery is None:
            return False
        attempt = session.scalar(
            select(Attempt).where(Attempt.lease_token == token, Attempt.event_id == event_id)
        )
        delivered = status is not None and 200 <= status < 300
        should_retry = (
            not delivered and retryable(status, error) and delivery.attempts_used < MAX_ATTEMPTS
        )
        delivery.status = "delivered" if delivered else "retry_wait" if should_retry else "failed"
        if session.get(Event, event_id).scope_id == "demo":
            retry_profile = "demo"
        if should_retry:
            delivery.next_attempt_at = now + timedelta(
                seconds=retry_delay(
                    delivery.attempts_used,
                    retry_profile,
                    status,
                    retry_after,
                    now,
                    jitter() if retry_profile == "production" else 0,
                )
            )
        delivery.last_error_code = error
        delivery.delivered_at = now if delivered else None
        delivery.updated_at = now
        delivery.lease_token = None
        delivery.lease_expires_at = None
        attempt.ended_at = now
        # An individual failed attempt can still lead to a later successful delivery.
        attempt.outcome = "delivered" if delivered else "failed"
        attempt.http_status = status
        attempt.duration_ms = duration
    return True


async def run_once(sessions, settings, client, *, clock=utc_now, jitter=random.random):
    with sessions.begin() as session:
        session.execute(
            insert(WorkerHeartbeat)
            .values(worker_id="delivery", last_seen_at=clock(), version="0.1.0")
            .on_conflict_do_update(
                index_elements=[WorkerHeartbeat.worker_id],
                set_={"last_seen_at": clock(), "version": "0.1.0"},
            )
        )
    recover_expired(sessions, now=clock(), retry_profile=settings.retry_profile, jitter=jitter)
    job = claim(sessions, settings.lease_duration, now=clock())
    if job is None:
        return False
    event_id, _, number, body = job
    timestamp = str(int(clock().timestamp()))
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Id": str(event_id),
        "X-Webhook-Attempt": str(number),
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": signature(
            settings.signing_secret.get_secret_value(), timestamp, body
        ),
    }
    start, status, error = time.monotonic(), None, None
    retry_after = None
    try:
        # HTTPX phase timeouts do not impose an overall deadline; asyncio does.
        async with asyncio.timeout(settings.request_deadline):
            async with client.stream(
                "POST", settings.destination_url, content=body, headers=headers
            ) as response:
                status = response.status_code
                retry_after = response.headers.get("Retry-After")
                if not 200 <= status < 300:
                    error = "http_error"
                # The response body is intentionally not buffered or logged.
    except (TimeoutError, httpx.TimeoutException):
        status, error = None, "timeout"
    except httpx.HTTPError:
        status, error = None, "connection_error"
    duration = int((time.monotonic() - start) * 1000)
    saved = finalize(
        sessions,
        job,
        status,
        error,
        duration,
        retry_profile=settings.retry_profile,
        retry_after=retry_after,
        now=clock(),
        jitter=jitter,
    )
    logger.info(
        json.dumps(
            {
                "event_id": str(event_id),
                "attempt": number,
                "outcome": error or "delivered",
                "duration_ms": duration,
                "finalized": saved,
            }
        )
    )
    return True


async def main():
    settings = Settings()
    engine, sessions = database(settings.database_url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    backoff = 1
    next_cleanup = 0
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10, connect=3), follow_redirects=False, trust_env=False
        ) as client:
            while not stop.is_set():
                try:
                    if time.monotonic() >= next_cleanup:
                        cleanup(sessions)
                        next_cleanup = time.monotonic() + 60
                    worked = await run_once(sessions, settings, client)
                    backoff = 1
                    if worked:
                        continue
                except SQLAlchemyError:
                    logger.warning('{"outcome":"database_unavailable"}')
                    backoff = min(backoff * 2, 30)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
