from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ansina.logging.context import request_id_scope
from ansina.logging.redaction import register_secret

logger = logging.getLogger("ansina.tests.formatter")


def test_emitted_line_is_a_single_json_object(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    logger.info("hello world")

    (record,) = captured_logs()
    assert record["level"] == "INFO"
    assert record["logger"] == "ansina.tests.formatter"
    assert record["message"] == "hello world"
    assert "timestamp" in record


def test_request_id_present_only_inside_scope(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    logger.info("outside scope")
    with request_id_scope("abc-123"):
        logger.info("inside scope")

    outside, inside = captured_logs()
    assert "request_id" not in outside
    assert inside["request_id"] == "abc-123"


def test_exception_field_present_on_log_exception(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")

    (record,) = captured_logs()
    assert "ValueError: boom" in record["exception"]


def test_secret_in_message_is_redacted(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    logger.info("Authorization: Bearer super-secret-token-value")

    (record,) = captured_logs()
    assert "super-secret-token-value" not in record["message"]


def test_secret_in_percent_args_is_redacted(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    logger.info(
        "auth failed for header %s", "Authorization: Bearer super-secret-arg-value"
    )

    (record,) = captured_logs()
    assert "super-secret-arg-value" not in record["message"]


def test_secret_in_extra_is_redacted(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    logger.info("request completed", extra={"api_key": "sk-abcdefghijklmnopqrstuvwx"})

    (record,) = captured_logs()
    assert "sk-abcdefghijklmnopqrstuvwx" not in record["extra"]["api_key"]


def test_secret_in_exception_message_is_redacted(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    try:
        raise ValueError("token=abcdef1234567890 rejected")
    except ValueError:
        logger.exception("auth error")

    (record,) = captured_logs()
    assert "abcdef1234567890" not in record["exception"]


def test_registered_literal_secret_is_redacted_in_message(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    register_secret("opaque-registered-secret-value")
    logger.info("saw value opaque-registered-secret-value in payload")

    (record,) = captured_logs()
    assert "opaque-registered-secret-value" not in record["message"]


def test_extra_survives_when_not_a_secret(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    logger.info("user action", extra={"user": "lucia", "count": 3})

    (record,) = captured_logs()
    assert record["extra"] == {"user": "lucia", "count": 3}


def test_secret_in_extra_list_is_redacted(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    logger.info(
        "tokens seen",
        extra={"tokens": ["token=abcdef1234567890", "harmless"]},
    )

    (record,) = captured_logs()
    assert record["extra"]["tokens"] == ["token=***", "harmless"]


def test_non_json_native_extra_is_stringified_and_redacted(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    class _Opaque:
        def __str__(self) -> str:
            return "token=abcdef1234567890"

    logger.info("opaque value", extra={"payload": _Opaque()})

    (record,) = captured_logs()
    assert record["extra"]["payload"] == "token=***"


def test_stack_info_is_included_and_redacted(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    logger.info("token=abcdef1234567890 stack requested", stack_info=True)

    (record,) = captured_logs()
    assert "stack" in record
    assert "abcdef1234567890" not in record["stack"]
