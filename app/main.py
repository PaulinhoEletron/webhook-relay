import html
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.security import APIKeyHeader
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse

from app.api.middleware import RequestBoundary, error_response
from app.api.schemas import Accepted, AttemptView, DemoInput, ErrorResponse, EventInput, EventStatus
from app.config import Settings
from app.db.models import Attempt, Delivery, DemoRun, Event, WorkerHeartbeat
from app.db.session import database
from app.services.demo import create_run, token_hash
from app.services.ingestion import IdempotencyConflict, ingest
from app.services.sandbox import client_key, poll


def create_app(settings=None):
    settings = settings or Settings()
    engine, sessions = database(settings.database_url)
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()

    app = FastAPI(title="Webhook Relay", version="0.1.0", lifespan=lifespan)
    static = Path(__file__).parent / "static"
    page_headers = {
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }

    @app.get("/", include_in_schema=False)
    def demo_page():
        return FileResponse(static / "index.html", headers=page_headers)

    @app.get("/assets/{name}", include_in_schema=False)
    def demo_asset(name: str):
        if name not in {"demo.css", "demo.js"}:
            raise HTTPException(404, "not_found")
        return FileResponse(static / name, headers=page_headers)

    sources = {
        "ingestion": "services/ingestion.py",
        "worker": "worker/main.py",
        "retries": "worker/retry.py",
        "schema": "db/models.py",
        "sandbox": "services/sandbox.py",
    }

    @app.get("/source", response_class=HTMLResponse, include_in_schema=False)
    def source_index():
        links = "".join(
            f'<li><a href="/source/{key}">{html.escape(path)}</a></li>'
            for key, path in sources.items()
        )
        return HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Webhook Relay source</title><link rel="stylesheet" href="/assets/demo.css">'
            "<main><h1>Source preview</h1><p>Selected files from this running application. "
            "The GitHub repository link will be added when the project is published.</p>"
            f'<ul>{links}</ul><a href="/">Back to the demo</a></main></html>',
            headers=page_headers,
        )

    @app.get("/source/{name}", include_in_schema=False)
    def source_file(name: str):
        if name not in sources:
            raise HTTPException(404, "not_found")
        return PlainTextResponse(
            (Path(__file__).parent / sources[name]).read_text(), headers=page_headers
        )

    app.state.sessions = sessions
    app.add_middleware(RequestBoundary)
    api_key = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authorize(key: Annotated[str | None, Depends(api_key)]):
        if key is None or not secrets.compare_digest(
            key.encode(), settings.operator_api_key.get_secret_value().encode()
        ):
            raise HTTPException(401, "unauthorized")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc):
        messages = {
            "quota_exceeded": "Sandbox limit reached; retry later",
            "unauthorized": "A valid API key is required",
            "not_found": "Event not found",
            "idempotency_conflict": "Key already used for a different request",
            "invalid_request": "Request validation failed",
        }
        code = exc.detail if exc.detail in messages else "http_error"
        response = error_response(
            code, messages.get(code, "Request failed"), request.state.request_id, exc.status_code
        )
        response.headers.update(exc.headers or {})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc):
        return error_response(
            "invalid_request", "Request validation failed", request.state.request_id, 422
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc):
        return error_response(
            "service_unavailable", "Database unavailable", request.state.request_id, 503
        )

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", responses={503: {"model": ErrorResponse}})
    def ready(request: Request):
        with engine.connect() as connection:
            revisions = (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            )
        if revisions != [head]:
            return error_response(
                "service_unavailable", "Migrations are not current", request.state.request_id, 503
            )
        return {"status": "ready"}

    errors = {code: {"model": ErrorResponse} for code in (401, 404, 409, 413, 422, 429, 503)}
    protected = {"dependencies": [Depends(authorize)], "responses": errors}

    @app.post(
        "/v1/events",
        response_model=Accepted,
        status_code=202,
        dependencies=[Depends(authorize)],
        responses={**errors, 200: {"model": Accepted, "description": "Idempotent replay"}},
    )
    def accept(
        payload: EventInput,
        response: Response,
        idempotency_key: Annotated[
            str, Header(min_length=1, max_length=128, pattern=r"^[\x20-\x7e]+$")
        ],
    ):
        try:
            result, replayed = ingest(sessions, payload, idempotency_key)
        except IdempotencyConflict:
            raise HTTPException(409, "idempotency_conflict") from None
        response.headers["Location"] = result["status_url"]
        if replayed:
            response.status_code = 200
            response.headers["Idempotency-Replayed"] = "true"
        return result

    def existing(session, event_id):
        event = session.get(Event, event_id)
        if event is None or event.scope_id != "operator":
            raise HTTPException(404, "not_found")
        return event

    @app.get("/v1/events/{event_id}", response_model=EventStatus, **protected)
    def status(event_id: UUID):
        with sessions() as session:
            event = existing(session, event_id)
            delivery = session.get(Delivery, event_id)
            return {
                "event_id": event.id,
                "status": delivery.status,
                "status_url": f"/v1/events/{event.id}",
                "created_at": event.created_at,
                "attempts_used": delivery.attempts_used,
                "last_error_code": delivery.last_error_code,
                "delivered_at": delivery.delivered_at,
            }

    @app.get("/v1/events/{event_id}/attempts", response_model=list[AttemptView], **protected)
    def attempts(event_id: UUID):
        with sessions() as session:
            existing(session, event_id)
            return [
                AttemptView.model_validate(row)
                for row in session.scalars(
                    select(Attempt)
                    .where(Attempt.event_id == event_id)
                    .order_by(Attempt.attempt_number)
                )
            ]

    @app.get("/health/worker")
    def worker_health():
        with sessions() as session:
            seen = session.scalar(
                select(WorkerHeartbeat.last_seen_at)
                .order_by(WorkerHeartbeat.last_seen_at.desc())
                .limit(1)
            )
        fresh = seen is not None and seen > datetime.now(UTC) - timedelta(seconds=30)
        return {"status": "online" if fresh else "offline", "last_seen_at": seen}

    @app.post("/demo/runs", status_code=202)
    def demo_create(payload: DemoInput, response: Response, request: Request):
        if not settings.demo_mode:
            raise HTTPException(404, "not_found")
        response.headers["Cache-Control"] = "no-store"
        return create_run(sessions, payload.scenario, client_key(request, settings), settings)

    @app.get("/demo/runs/{run_id}")
    def demo_status(
        run_id: UUID,
        response: Response,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ):
        if not settings.demo_mode:
            raise HTTPException(404, "not_found")
        poll(sessions, client_key(request, settings))
        token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        with sessions() as session:
            run = session.get(DemoRun, run_id)
            if (
                run is None
                or not token
                or not secrets.compare_digest(run.token_hash, token_hash(token))
                or run.expires_at <= datetime.now(UTC)
            ):
                raise HTTPException(401, "unauthorized")
            event = session.get(Event, run.event_id)
            delivery = session.get(Delivery, run.event_id)
            response.headers["Cache-Control"] = "no-store"
            return {
                "run_id": run.id,
                "event_id": event.id,
                "scenario": run.scenario,
                "created_at": event.created_at,
                "expires_at": run.expires_at,
                "status": delivery.status,
                "attempts_used": delivery.attempts_used,
                "delivered_at": delivery.delivered_at,
                "attempts": [
                    AttemptView.model_validate(row)
                    for row in session.scalars(
                        select(Attempt)
                        .where(Attempt.event_id == event.id)
                        .order_by(Attempt.attempt_number)
                    )
                ],
            }

    return app
