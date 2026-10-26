import pytest
from pydantic import ValidationError


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:9000",
        "http://receiver:8001/other",
        "https://user:password@example.com",
        "ftp://example.com",
    ],
)
def test_unsafe_destinations_rejected(settings, url):
    with pytest.raises(ValidationError):
        type(settings)(**{**settings.model_dump(), "destination_url": url})


def test_internal_exception_requires_demo_mode(settings):
    with pytest.raises(ValidationError):
        type(settings)(**{**settings.model_dump(), "demo_mode": False})


def test_retry_profile_defaults_to_production(settings):
    assert settings.retry_profile == "production"


@pytest.mark.parametrize("profile", ["unknown", "", "PRODUCTION"])
def test_unknown_retry_profile_rejected(settings, profile):
    with pytest.raises(ValidationError):
        type(settings)(**{**settings.model_dump(), "retry_profile": profile})


def test_accelerated_profile_requires_demo_mode(settings):
    with pytest.raises(ValidationError):
        type(settings)(
            **{
                **settings.model_dump(),
                "retry_profile": "demo",
                "demo_mode": False,
                "destination_url": "https://example.com/webhooks",
            }
        )
