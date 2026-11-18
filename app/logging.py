"""JSON logging shared by the API, worker and receiver."""

import json
import logging
import sys
from datetime import UTC, datetime

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "taskName",
    "color_message",
}
_OWNED = "_relay_json_handler"


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level=logging.INFO):
    """Route every logger, including uvicorn's, through one JSON handler (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(handler, _OWNED, False) for handler in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        setattr(handler, _OWNED, True)
        root.addHandler(handler)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # The request middleware writes the access line (route template, hashed client);
    # uvicorn's own line would add the raw peer address and path.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
