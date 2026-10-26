import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.api.middleware import RequestBoundary
from app.config import Settings
from app.db.models import DemoRun, FulfillmentEffect
from app.db.session import database
from app.signing import signature

logger = logging.getLogger("relay.receiver")


def create_app(settings=None):
    settings = settings or Settings()
    engine, sessions = database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()

    app = FastAPI(title="Example fulfillment receiver", lifespan=lifespan)
    app.state.sessions = sessions

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse({"detail": "Receiver database unavailable"}, status_code=503)

    def fulfill(event_id, body):
        # The inserted ledger row IS the simulated fulfillment effect. Its unique ID
        # makes duplicates no-ops, including concurrent requests and lost responses.
        with sessions.begin() as session:
            run = session.scalar(
                select(DemoRun).where(DemoRun.event_id == UUID(event_id)).with_for_update()
            )
            if run is not None and settings.demo_mode:
                run.receipts += 1
                if run.scenario == "permanent_failure":
                    return 400, False
                if run.scenario == "temporary_failure" and run.receipts <= 2:
                    return 503, False
            return 200, (
                session.scalar(
                    insert(FulfillmentEffect)
                    .values(event_id=UUID(event_id), outbound_body=body)
                    .on_conflict_do_nothing(index_elements=[FulfillmentEffect.event_id])
                    .returning(FulfillmentEffect.event_id)
                )
                is not None
            )

    app.add_middleware(RequestBoundary, max_body=32 * 1024)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.post("/webhooks")
    async def webhook(request: Request):
        body = await request.body()
        timestamp = request.headers.get("X-Webhook-Timestamp", "")
        received = request.headers.get("X-Webhook-Signature", "")
        try:
            valid_time = abs(time.time() - int(timestamp)) <= 300
        except ValueError:
            valid_time = False
        expected = signature(settings.signing_secret.get_secret_value(), timestamp, body)
        if not valid_time or not hmac.compare_digest(received.encode(), expected.encode()):
            raise HTTPException(401, "Invalid signature")
        try:
            envelope = json.loads(body)
            event_id = str(UUID(envelope["id"]))
            if event_id != request.headers.get("X-Webhook-Id"):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "Invalid event envelope") from None
        status, created = await run_in_threadpool(fulfill, event_id, body)
        if status != 200:
            return JSONResponse({"detail": "Demo scenario response"}, status_code=status)
        logger.info(
            json.dumps({"event_id": event_id, "outcome": "fulfilled" if created else "duplicate"})
        )
        return {"received": event_id}

    return app
