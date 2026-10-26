from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from ansina.logging.redaction import clear_secrets, register_secret
from ansina.opencode.events import (
    RunEvent,
    RunEventType,
    RunOutcome,
    RunRequest,
    RunStatus,
    _truncate_utf8,
    parse_run_event,
)


@pytest.fixture(autouse=True)
def _clear_redaction_registry() -> Iterator[None]:
    """`ansina.logging.redaction`'s registered-secret set is process-global —
    clear it before and after every test here so a secret registered by one
    test can never leak into another's assertions. Local to this file (not
    the shared `tests/unit/logging/conftest.py` fixture, which is scoped to
    that directory only), the same locally-register-and-clear shape
    `tests/unit/telemetry/test_log_mirror.py` already uses.
    """
    clear_secrets()
    yield
    clear_secrets()


# --- parse_run_event --------------------------------------------------------


def test_blank_line_returns_none() -> None:
    assert parse_run_event("", max_event_bytes=1024, sequence=0) is None


def test_whitespace_only_line_returns_none() -> None:
    assert parse_run_event("   \n", max_event_bytes=1024, sequence=0) is None


def test_non_json_line_returns_none() -> None:
    assert parse_run_event("not json at all", max_event_bytes=1024, sequence=0) is None


def test_json_array_line_returns_none() -> None:
    """A valid JSON document that isn't an object has no `type`/`event` key
    to classify by — treated the same as unparseable, not a crash.
    """
    assert parse_run_event("[1, 2, 3]", max_event_bytes=1024, sequence=0) is None


def test_json_scalar_line_returns_none() -> None:
    assert parse_run_event("42", max_event_bytes=1024, sequence=0) is None


def test_recognized_object_normalizes_to_other_with_raw_type_preserved() -> None:
    """The lookup table is intentionally empty today (see module docstring) —
    every `type` value normalizes to `OTHER`, but `raw_type` still carries
    the real, as-emitted string, so no information is lost.
    """
    line = json.dumps({"type": "tool_call", "detail": "ls -la"})

    event = parse_run_event(line, max_event_bytes=1024, sequence=3)

    assert event is not None
    assert event.type is RunEventType.OTHER
    assert event.raw_type == "tool_call"
    assert event.sequence == 3
    assert "tool_call" in event.payload


def test_falls_back_to_event_key_when_type_is_absent() -> None:
    line = json.dumps({"event": "server.connected"})

    event = parse_run_event(line, max_event_bytes=1024, sequence=0)

    assert event is not None
    assert event.raw_type == "server.connected"


def test_type_key_takes_priority_over_event_key() -> None:
    line = json.dumps({"type": "a", "event": "b"})

    event = parse_run_event(line, max_event_bytes=1024, sequence=0)

    assert event is not None
    assert event.raw_type == "a"


def test_non_string_type_falls_back_to_event_key() -> None:
    line = json.dumps({"type": 123, "event": "b"})

    event = parse_run_event(line, max_event_bytes=1024, sequence=0)

    assert event is not None
    assert event.raw_type == "b"


def test_no_type_or_event_key_yields_empty_raw_type() -> None:
    line = json.dumps({"detail": "nothing recognizable here"})

    event = parse_run_event(line, max_event_bytes=1024, sequence=0)

    assert event is not None
    assert event.raw_type == ""
    assert event.type is RunEventType.OTHER


def test_payload_is_truncated_to_max_event_bytes() -> None:
    line = json.dumps({"type": "x", "detail": "a" * 1000})

    event = parse_run_event(line, max_event_bytes=32, sequence=0)

    assert event is not None
    assert len(event.payload.encode("utf-8")) <= 32


def test_payload_untruncated_when_within_budget() -> None:
    line = json.dumps({"type": "x"})

    event = parse_run_event(line, max_event_bytes=4096, sequence=0)

    assert event is not None
    assert event.payload == line


def test_payload_redacts_a_registered_secret() -> None:
    register_secret("sk-super-secret-value")
    line = json.dumps({"type": "x", "token": "sk-super-secret-value"})

    event = parse_run_event(line, max_event_bytes=4096, sequence=0)

    assert event is not None
    assert "sk-super-secret-value" not in event.payload


def test_classification_uses_the_untruncated_original_line() -> None:
    """A `max_event_bytes` small enough that truncating the raw text *before*
    parsing would cut this line mid-object (landing inside the `"pad"` string
    value, making it invalid JSON) must not make this line "malformed" —
    parsing happens against the original, untruncated text; only the
    `payload` that ends up on the returned event is capped afterward.
    """
    line = json.dumps({"type": "a", "pad": "x" * 100})
    assert len(line.encode("utf-8")) > 8  # the whole line exceeds the cap below

    event = parse_run_event(line, max_event_bytes=8, sequence=0)

    assert event is not None
    assert len(event.payload.encode("utf-8")) <= 8


# --- _truncate_utf8 (direct, for the UTF-8 boundary-safety edge case) ------


def test_truncate_utf8_returns_text_unchanged_when_within_budget() -> None:
    assert _truncate_utf8("hello", max_bytes=100) == "hello"


def test_truncate_utf8_drops_a_multibyte_char_split_at_the_boundary() -> None:
    """ "é" is 2 UTF-8 bytes (0xC3 0xA9) — truncating to 1 byte must not split
    it and raise; `errors="ignore"` drops the incomplete trailing byte.
    """
    text = "aé"  # b"a\xc3\xa9"

    truncated = _truncate_utf8(text, max_bytes=2)

    assert truncated == "a"
    # Never raises, and the result is always valid text.
    truncated.encode("utf-8")


# --- RunRequest --------------------------------------------------------------


def test_run_request_defaults() -> None:
    request = RunRequest(prompt="hello")

    assert request.prompt == "hello"
    assert request.session_id is None
    assert request.continue_last is False
    assert request.fork is False
    assert request.model is None
    assert request.agent is None
    assert request.auto_approve is None


def test_run_request_rejects_session_id_and_continue_last_together() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        RunRequest(prompt="hi", session_id="abc", continue_last=True)


def test_run_request_rejects_fork_with_neither_session_id_nor_continue() -> None:
    with pytest.raises(ValueError, match="fork=True requires"):
        RunRequest(prompt="hi", fork=True)


def test_run_request_accepts_fork_with_session_id() -> None:
    request = RunRequest(prompt="hi", session_id="abc", fork=True)

    assert request.fork is True


def test_run_request_accepts_fork_with_continue_last() -> None:
    request = RunRequest(prompt="hi", continue_last=True, fork=True)

    assert request.fork is True


# --- RunEventType / RunStatus / RunOutcome / RunEvent -----------------------


def test_run_event_type_other_value() -> None:
    assert RunEventType.OTHER.value == "other"


def test_run_status_values() -> None:
    assert RunStatus.COMPLETED.value == "completed"
    assert RunStatus.FAILED.value == "failed"
    assert RunStatus.TIMEOUT.value == "timeout"


def test_run_outcome_defaults() -> None:
    outcome = RunOutcome(
        status=RunStatus.COMPLETED,
        exit_code=0,
        events_emitted=3,
        events_skipped=0,
        duration_seconds=1.5,
    )

    assert outcome.truncated is False
    assert outcome.error is None


def test_run_event_fields() -> None:
    event = RunEvent(type=RunEventType.OTHER, raw_type="x", payload="{}", sequence=5)

    assert event.type is RunEventType.OTHER
    assert event.raw_type == "x"
    assert event.payload == "{}"
    assert event.sequence == 5
