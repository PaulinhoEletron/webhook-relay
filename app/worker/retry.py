"""Bounded retry policy; callers supply time and randomness for deterministic checks."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

MAX_ATTEMPTS = 5
MAX_DELAY = 15 * 60


def retryable(status: int | None, error: str | None) -> bool:
    return (
        error in {"timeout", "connection_error"}
        or status in {408, 429}
        or (status is not None and 500 <= status <= 599)
    )


def retry_after_seconds(value: str | None, now: datetime) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        # Clamp before converting arbitrarily large server-supplied integers.
        digits = value.lstrip("0") or "0"
        return float(MAX_DELAY if len(digits) > 3 else min(int(digits), MAX_DELAY))
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(0, min((date - now).total_seconds(), MAX_DELAY))
    except (TypeError, ValueError, OverflowError):
        return None


def retry_delay(
    attempt: int,
    profile: str,
    status: int | None,
    retry_after: str | None,
    now: datetime,
    jitter: float,
) -> float:
    if not 1 <= attempt < MAX_ATTEMPTS:
        raise ValueError("Retry delay requires an unexhausted attempt budget")
    if profile == "demo":
        delay = (2, 4, 8, 12)[attempt - 1]
    elif profile == "production":
        delay = (5, 15, 45, 120)[attempt - 1] * (1 + 0.2 * jitter)
    else:
        raise ValueError("Unknown retry profile")
    suggested = retry_after_seconds(retry_after, now) if status in {429, 503} else None
    return min(max(delay, suggested or 0), MAX_DELAY)
