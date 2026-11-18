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


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/x/y",
        "javascript:alert(1)",
        "ftp://x.test",
        "https://u:p@x.test",
        "https://",
    ],
)
def test_source_url_must_be_https(settings, url):
    with pytest.raises(ValidationError):
        type(settings)(**{**settings.model_dump(), "source_url": url})


def test_source_url_may_be_empty_or_https(settings):
    for url in ("", "https://github.com/PaulinhoEletron/webhook-relay"):
        assert type(settings)(**{**settings.model_dump(), "source_url": url}).source_url == url


def test_edge_defaults(settings):
    assert settings.trust_cf_connecting_ip is False
    assert settings.body_read_timeout == 10
    assert settings.source_url == ""
