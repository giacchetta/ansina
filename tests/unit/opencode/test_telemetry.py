from __future__ import annotations

import json
from pathlib import Path

from ansina.config.settings import OpencodeSettings, Settings, TelemetrySettings
from ansina.logging.redaction import clear_secrets, register_secret
from ansina.opencode.events import (
    RunEvent,
    RunEventType,
    RunOutcome,
    RunRequest,
    RunStatus,
)
from ansina.opencode.telemetry import (
    OpencodeTelemetryWriter,
    build_opencode_telemetry_writer,
)
from ansina.telemetry.rotation import SpooledRotatingWriter


def _settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    """Same `Settings(telemetry=..., opencode=...)` shape every other
    `ansina.opencode` test module uses (`tests/unit/opencode/test_workspace.py`'s
    own `_settings` docstring explains why both are passed as complete model
    instances).
    """
    return Settings(
        telemetry=TelemetrySettings(enabled=True),
        opencode=OpencodeSettings(
            enabled=enabled, workspace_dir=tmp_path / "workspace"
        ),
    )


def _make_writer(
    tmp_path: Path, *, max_line_bytes: int = 65_536
) -> OpencodeTelemetryWriter:
    writer = SpooledRotatingWriter(
        spool_dir=tmp_path,
        file_prefix="opencode",
        max_file_bytes=10_000_000,
        max_spool_bytes=10_000_000,
        retention_hours=24.0,
    )
    return OpencodeTelemetryWriter(writer=writer, max_line_bytes=max_line_bytes)


def _read_lines(tmp_path: Path) -> list[dict[str, object]]:
    active = tmp_path / "opencode.jsonl"
    if not active.exists():
        return []
    return [json.loads(line) for line in active.read_text().splitlines()]


def test_record_submitted_writes_prompt_preview_and_request_fields(
    tmp_path: Path,
) -> None:
    writer = _make_writer(tmp_path)
    request = RunRequest(prompt="say hello", model="claude", agent="build", fork=False)

    writer.record_submitted("run-1", "session-1", request)

    [line] = _read_lines(tmp_path)
    assert line["event"] == "submitted"
    assert line["run_id"] == "run-1"
    assert line["session_id"] == "session-1"
    assert line["seq"] == 0
    assert isinstance(line["t"], float)
    assert line["prompt"] == "say hello"
    assert line["model"] == "claude"
    assert line["agent"] == "build"
    assert line["continue_last"] is False
    assert line["fork"] is False
    assert line["auto_approve"] is None


def test_record_started_writes_no_fields_beyond_the_envelope(tmp_path: Path) -> None:
    writer = _make_writer(tmp_path)

    writer.record_started("run-1", None)

    [line] = _read_lines(tmp_path)
    assert line["event"] == "started"
    assert line["session_id"] is None
    assert set(line) == {"t", "run_id", "session_id", "seq", "event"}


def test_record_event_carries_raw_type_and_payload(tmp_path: Path) -> None:
    writer = _make_writer(tmp_path)
    event = RunEvent(
        type=RunEventType.OTHER, raw_type="step", payload='{"detail": "x"}', sequence=0
    )

    writer.record_event("run-1", "session-1", event)

    [line] = _read_lines(tmp_path)
    assert line["event"] == "run_event"
    assert line["raw_type"] == "step"
    assert line["payload"] == '{"detail": "x"}'


def test_record_event_extracts_known_fields_from_a_tool_call_payload(
    tmp_path: Path,
) -> None:
    writer = _make_writer(tmp_path)
    payload = json.dumps(
        {"type": "tool_call", "tool": "bash", "command": "ls", "path": "/tmp"}
    )
    event = RunEvent(
        type=RunEventType.OTHER, raw_type="tool_call", payload=payload, sequence=0
    )

    writer.record_event("run-1", None, event)

    [line] = _read_lines(tmp_path)
    assert line["tool"] == "bash"
    assert line["command"] == "ls"
    assert line["path"] == "/tmp"


def test_record_event_extracts_permission_denial_fields(tmp_path: Path) -> None:
    writer = _make_writer(tmp_path)
    payload = json.dumps({"permission": "bash", "pattern": "rm *", "decision": "deny"})
    event = RunEvent(
        type=RunEventType.OTHER, raw_type="permission", payload=payload, sequence=0
    )

    writer.record_event("run-1", None, event)

    [line] = _read_lines(tmp_path)
    assert line["permission"] == "bash"
    assert line["pattern"] == "rm *"
    assert line["decision"] == "deny"


def test_record_event_extracts_usage_fields(tmp_path: Path) -> None:
    writer = _make_writer(tmp_path)
    payload = json.dumps({"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.002})
    event = RunEvent(
        type=RunEventType.OTHER, raw_type="usage", payload=payload, sequence=0
    )

    writer.record_event("run-1", None, event)

    [line] = _read_lines(tmp_path)
    assert line["prompt_tokens"] == 10
    assert line["completion_tokens"] == 5
    assert line["cost"] == 0.002


def test_record_event_with_unparseable_payload_keeps_raw_type_and_payload_only(
    tmp_path: Path,
) -> None:
    writer = _make_writer(tmp_path)
    event = RunEvent(
        type=RunEventType.OTHER, raw_type="", payload="not valid json {", sequence=0
    )

    writer.record_event("run-1", None, event)

    [line] = _read_lines(tmp_path)
    assert line["payload"] == "not valid json {"
    for key in (
        "tool",
        "command",
        "path",
        "permission",
        "pattern",
        "decision",
        "prompt_tokens",
        "completion_tokens",
        "cost",
    ):
        assert key not in line


def test_record_event_with_a_non_object_json_payload_extracts_nothing(
    tmp_path: Path,
) -> None:
    """A payload that parses as valid JSON but isn't an object (e.g. a bare
    array or scalar — OpenCode's own event vocabulary is unverified, so this
    shape isn't ruled out) must not raise and must extract no known fields.
    """
    writer = _make_writer(tmp_path)
    event = RunEvent(
        type=RunEventType.OTHER, raw_type="", payload="[1, 2, 3]", sequence=0
    )

    writer.record_event("run-1", None, event)

    [line] = _read_lines(tmp_path)
    assert line["payload"] == "[1, 2, 3]"
    assert "tool" not in line


def test_record_outcome_completed_writes_matching_event_and_fields(
    tmp_path: Path,
) -> None:
    writer = _make_writer(tmp_path)
    outcome = RunOutcome(
        status=RunStatus.COMPLETED,
        exit_code=0,
        events_emitted=3,
        events_skipped=1,
        duration_seconds=1.5,
        truncated=False,
        error=None,
    )

    writer.record_outcome("run-1", "session-1", outcome)

    [line] = _read_lines(tmp_path)
    assert line["event"] == "completed"
    assert line["exit_code"] == 0
    assert line["duration_seconds"] == 1.5
    assert line["events_emitted"] == 3
    assert line["events_skipped"] == 1
    assert line["truncated"] is False
    assert line["error"] is None


def test_record_outcome_failed_and_timeout_write_their_own_status(
    tmp_path: Path,
) -> None:
    writer = _make_writer(tmp_path)
    failed = RunOutcome(
        status=RunStatus.FAILED,
        exit_code=1,
        events_emitted=0,
        events_skipped=0,
        duration_seconds=0.1,
        error="opencode exited with code 1",
    )
    timeout = RunOutcome(
        status=RunStatus.TIMEOUT,
        exit_code=None,
        events_emitted=0,
        events_skipped=0,
        duration_seconds=900.0,
        error="run exceeded opencode.run_timeout_seconds=900.0",
    )

    writer.record_outcome("run-1", None, failed)
    writer.record_outcome("run-2", None, timeout)

    lines = _read_lines(tmp_path)
    assert lines[0]["event"] == "failed"
    assert lines[0]["error"] == "opencode exited with code 1"
    assert lines[1]["event"] == "timeout"
    assert lines[1]["exit_code"] is None


def test_record_cancelled_writes_its_own_event_and_duration(tmp_path: Path) -> None:
    writer = _make_writer(tmp_path)

    writer.record_cancelled("run-1", "session-1", duration_seconds=2.25)

    [line] = _read_lines(tmp_path)
    assert line["event"] == "cancelled"
    assert line["duration_seconds"] == 2.25


def test_seq_is_monotonic_per_run_and_resets_after_a_terminal_event(
    tmp_path: Path,
) -> None:
    writer = _make_writer(tmp_path)
    request = RunRequest(prompt="hi")
    event = RunEvent(type=RunEventType.OTHER, raw_type="x", payload="{}", sequence=0)
    outcome = RunOutcome(
        status=RunStatus.COMPLETED,
        exit_code=0,
        events_emitted=1,
        events_skipped=0,
        duration_seconds=1.0,
    )

    writer.record_submitted("run-1", None, request)
    writer.record_started("run-1", None)
    writer.record_event("run-1", None, event)
    writer.record_event("run-1", None, event)
    writer.record_outcome("run-1", None, outcome)
    # A second run reusing the same run_id string after the first one's
    # terminal event starts its own seq back at 0 — proves the internal
    # per-run counter was popped, not merely incremented further.
    writer.record_submitted("run-1", None, request)

    lines = _read_lines(tmp_path)
    assert [line["seq"] for line in lines[:5]] == [0, 1, 2, 3, 4]
    assert lines[5]["seq"] == 0


def test_seq_counters_are_independent_across_concurrent_runs(tmp_path: Path) -> None:
    writer = _make_writer(tmp_path)
    request = RunRequest(prompt="hi")

    writer.record_submitted("run-a", None, request)
    writer.record_submitted("run-b", None, request)
    writer.record_started("run-a", None)
    writer.record_started("run-b", None)

    lines = _read_lines(tmp_path)
    by_run = {(line["run_id"], line["event"]): line["seq"] for line in lines}
    assert by_run[("run-a", "submitted")] == 0
    assert by_run[("run-a", "started")] == 1
    assert by_run[("run-b", "submitted")] == 0
    assert by_run[("run-b", "started")] == 1


def test_a_registered_secret_in_the_prompt_is_masked_in_the_written_line(
    tmp_path: Path,
) -> None:
    writer = _make_writer(tmp_path)
    register_secret("super-secret-value-123456")
    try:
        request = RunRequest(prompt="use this token: super-secret-value-123456")
        writer.record_submitted("run-1", None, request)

        raw_line = (tmp_path / "opencode.jsonl").read_text().splitlines()[0]
        assert "super-secret-value-123456" not in raw_line
        [line] = _read_lines(tmp_path)
        assert "super-secret-value-123456" not in str(line["prompt"])
    finally:
        clear_secrets()


def test_a_registered_secret_in_an_event_payload_is_masked(tmp_path: Path) -> None:
    writer = _make_writer(tmp_path)
    register_secret("sk-another-secret-value")
    try:
        event = RunEvent(
            type=RunEventType.OTHER,
            raw_type="x",
            payload='{"detail": "sk-another-secret-value"}',
            sequence=0,
        )
        writer.record_event("run-1", None, event)

        raw_line = (tmp_path / "opencode.jsonl").read_text().splitlines()[0]
        assert "sk-another-secret-value" not in raw_line
    finally:
        clear_secrets()


def test_an_oversized_line_falls_back_to_the_minimal_envelope(tmp_path: Path) -> None:
    writer = _make_writer(tmp_path, max_line_bytes=80)
    request = RunRequest(prompt="x" * 1_000)

    writer.record_submitted("run-1", "session-1", request)

    [line] = _read_lines(tmp_path)
    assert line["event"] == "submitted"
    assert line["truncated"] is True
    assert "prompt" not in line
    assert (
        len((tmp_path / "opencode.jsonl").read_text().splitlines()[0].encode()) <= 200
    )


def test_a_write_failure_is_swallowed_and_logged_not_raised(tmp_path: Path) -> None:
    # A spool_dir that is actually a file makes `SpooledRotatingWriter.
    # write_line`'s own `mkdir` raise — the same `blocking_file` trick
    # `tests/unit/telemetry/test_log_mirror.py` already uses to force this.
    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("not a directory")
    broken = SpooledRotatingWriter(
        spool_dir=blocking_file / "spool",
        file_prefix="opencode",
        max_file_bytes=1_000_000,
        max_spool_bytes=1_000_000,
        retention_hours=24.0,
    )
    writer = OpencodeTelemetryWriter(writer=broken, max_line_bytes=65_536)

    # Must not raise.
    writer.record_started("run-1", None)


def test_build_opencode_telemetry_writer_is_none_when_opencode_disabled(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, enabled=False)

    assert build_opencode_telemetry_writer(settings) is None


def test_build_opencode_telemetry_writer_returns_a_real_writer_when_enabled(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, enabled=True)

    writer = build_opencode_telemetry_writer(settings)

    assert writer is not None
    writer.record_started("run-1", None)
    assert (settings.telemetry.spool_dir / "opencode.jsonl").exists()


def test_module_has_no_database_dependency() -> None:
    import ansina.opencode.telemetry as module

    assert "ansina.storage" not in {
        getattr(value, "__module__", None) for value in vars(module).values()
    }
    assert "Database" not in dir(module)
