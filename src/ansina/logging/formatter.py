"""`JsonFormatter` — one JSON object per log line, redacted before serialization.

Redaction happens here, not at call sites: every value that reaches `format()` is walked
and redacted *before* `json.dumps` runs, so nothing downstream of this formatter can log
a secret by forgetting to call `redact()` itself. This is the "redaction as first-class
formatter behavior" instinct the blueprint calls out (OpenClaw's `src/logging/`).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from ansina.logging.context import get_request_id
from ansina.logging.redaction import redact

# Attributes every stdlib `LogRecord` carries — anything else in `record.__dict__` came
# from a caller's `extra={...}` and is surfaced under the `"extra"` key.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)


def _redact_value(value: Any) -> Any:  # noqa: ANN401 — walks arbitrary `extra={}` payloads
    """Recursively redact `str` leaves of an arbitrary JSON-ish value.

    Non-JSON-native values (e.g. an object passed via `extra=`) are stringified and
    redacted too, rather than passed through to `json.dumps` where they'd raise.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


class JsonFormatter(logging.Formatter):
    """Formats each `LogRecord` as a single redacted JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }

        request_id = get_request_id()
        if request_id is not None:
            payload["request_id"] = request_id

        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_ATTRS and key not in payload
        }
        if extra:
            payload["extra"] = _redact_value(extra)

        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact(self.formatStack(record.stack_info))

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
