import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_type: str = Field(min_length=1, max_length=100)
    data: dict[str, JsonValue]

    @model_validator(mode="after")
    def serializable(self):
        try:
            canonical(self.model_dump())
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("A finite, UTF-8 JSON object is required") from exc
        return self


def canonical(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


Status = Literal["pending", "processing", "retry_wait", "delivered", "failed"]


class Accepted(BaseModel):
    event_id: UUID
    status: Status
    status_url: str
    created_at: datetime


class EventStatus(Accepted):
    attempts_used: int
    last_error_code: str | None
    delivered_at: datetime | None


class AttemptView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    attempt_number: int
    started_at: datetime
    ended_at: datetime | None
    outcome: str
    http_status: int | None
    duration_ms: int | None


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: UUID


class ErrorResponse(BaseModel):
    error: ErrorDetail


class DemoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: Literal["success", "temporary_failure", "permanent_failure"]
