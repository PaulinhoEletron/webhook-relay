import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings():
    url = os.environ["DATABASE_URL"]
    if make_url(url).database != "relay_test":
        pytest.fail("Tests require a dedicated database named relay_test")
    return Settings(
        database_url=url,
        operator_api_key="test-operator-key-" + "a" * 32,
        signing_secret="test-signing-key-" + "b" * 32,
    )


@pytest.fixture
def app(settings):
    app = create_app(settings)
    with app.state.sessions.begin() as session:
        session.execute(
            text(
                "TRUNCATE events, fulfillment_effects, worker_heartbeat, demo_poll_buckets CASCADE"
            )
        )
    return app


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def headers(settings):
    return {
        "X-API-Key": settings.operator_api_key.get_secret_value(),
        "Idempotency-Key": "order-1",
        "Content-Type": "application/json",
    }
