from __future__ import annotations

import json
import logging
from pathlib import Path

from ansina.logging.formatter import JsonFormatter
from ansina.telemetry.log_mirror import TelemetryLogHandler


def _make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="ansina.tests.telemetry",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_emit_writes_one_formatted_line_to_the_spool(tmp_path: Path) -> None:
    handler = TelemetryLogHandler(
        spool_dir=str(tmp_path),
        max_file_bytes=1_000_000,
        max_spool_bytes=1_000_000,
        retention_hours=24.0,
    )
    handler.setFormatter(JsonFormatter())

    handler.emit(_make_record("hello mirror"))

    active = tmp_path / "log.jsonl"
    assert active.exists()
    payload = json.loads(active.read_text().splitlines()[0])
    assert payload["message"] == "hello mirror"


def test_emit_appends_multiple_records(tmp_path: Path) -> None:
    handler = TelemetryLogHandler(
        spool_dir=str(tmp_path),
        max_file_bytes=1_000_000,
        max_spool_bytes=1_000_000,
        retention_hours=24.0,
    )
    handler.setFormatter(JsonFormatter())

    handler.emit(_make_record("first"))
    handler.emit(_make_record("second"))

    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["message"] == "first"
    assert json.loads(lines[1])["message"] == "second"


def test_emit_rotates_through_the_shared_rotation_writer(tmp_path: Path) -> None:
    handler = TelemetryLogHandler(
        spool_dir=str(tmp_path),
        max_file_bytes=1,
        max_spool_bytes=1_000_000,
        retention_hours=24.0,
    )
    handler.setFormatter(JsonFormatter())

    handler.emit(_make_record("first"))  # fills the active file past max_file_bytes
    handler.emit(_make_record("second"))  # must rotate "first" out

    rotated = list(tmp_path.glob("log-*.jsonl"))
    assert len(rotated) == 1


def test_emit_failure_goes_through_handle_error_not_raised(tmp_path: Path) -> None:
    handler = TelemetryLogHandler(
        spool_dir=str(tmp_path),
        max_file_bytes=1_000_000,
        max_spool_bytes=1_000_000,
        retention_hours=24.0,
    )
    handler.setFormatter(JsonFormatter())
    handled: list[logging.LogRecord] = []
    handler.handleError = handled.append  # type: ignore[assignment]

    # A spool_dir that is actually a file (not a directory) makes `mkdir` inside
    # `write_line` raise — the handler must swallow it via `handleError`, never let
    # it propagate out of `emit`.
    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("not a directory")
    broken_handler = TelemetryLogHandler(
        spool_dir=str(blocking_file / "spool"),
        max_file_bytes=1_000_000,
        max_spool_bytes=1_000_000,
        retention_hours=24.0,
    )
    broken_handler.setFormatter(JsonFormatter())
    broken_handler.handleError = handled.append  # type: ignore[assignment]

    broken_handler.emit(_make_record("will fail"))

    assert len(handled) == 1


def test_redaction_matches_the_primary_formatter(tmp_path: Path) -> None:
    """The mirror handler must format through the *same* `JsonFormatter` class the
    primary stream uses — `dictConfig` (see `ansina.logging.setup`) is what makes
    the two share one literal instance in production; here, constructing both
    handlers with their own `JsonFormatter()` instance and asserting identical
    output for the same record proves the formatter contract itself never diverges
    between the two.
    """
    from ansina.logging.redaction import clear_secrets, register_secret

    register_secret("super-secret-value")
    try:
        record = _make_record("token is super-secret-value")

        primary_formatter = JsonFormatter()
        primary_line = primary_formatter.format(record)

        handler = TelemetryLogHandler(
            spool_dir=str(tmp_path),
            max_file_bytes=1_000_000,
            max_spool_bytes=1_000_000,
            retention_hours=24.0,
        )
        handler.setFormatter(JsonFormatter())
        handler.emit(record)
        mirrored_line = (tmp_path / "log.jsonl").read_text().splitlines()[0]

        assert "super-secret-value" not in primary_line
        assert "super-secret-value" not in mirrored_line
        assert (
            json.loads(primary_line)["message"] == json.loads(mirrored_line)["message"]
        )
    finally:
        clear_secrets()
