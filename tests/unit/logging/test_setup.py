from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from ansina.config import load_settings
from ansina.logging.redaction import clear_secrets
from ansina.logging.setup import configure_logging, get_logger


def test_configure_logging_honours_configured_level(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_LOGGING__LEVEL", "WARNING")
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    configure_logging(load_settings())
    log = get_logger("ansina.tests.setup")
    log.info("dropped below WARNING")
    log.warning("kept at WARNING")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    messages = [line["message"] for line in lines]
    assert "kept at WARNING" in messages
    assert "dropped below WARNING" not in messages


def test_configure_logging_is_idempotent(clean_env: None, tmp_cwd: Path) -> None:
    configure_logging(load_settings())
    configure_logging(load_settings())

    root = logging.getLogger()
    ansina_handlers = [h for h in root.handlers if h.get_name() == "ansina.json"]
    assert len(ansina_handlers) == 1


def test_configure_logging_registers_configured_token(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #28: `api_token` requires `admin_username` alongside it.
    monkeypatch.setenv("ANSINA_SECURITY__ADMIN_USERNAME", "configured-admin")
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", "configured-secret-token-value-x1")
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    try:
        configure_logging(load_settings())
        log = get_logger("ansina.tests.setup")
        log.info("token in use: configured-secret-token-value-x1")

        assert "configured-secret-token-value-x1" not in stream.getvalue()
    finally:
        clear_secrets()


def test_configure_logging_registers_configured_brain_api_key(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_BRAIN__API_KEY", "configured-secret-brain-key-value")
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    try:
        configure_logging(load_settings())
        log = get_logger("ansina.tests.setup")
        log.info("key in use: configured-secret-brain-key-value")

        assert "configured-secret-brain-key-value" not in stream.getvalue()
    finally:
        clear_secrets()


# --- telemetry log mirror (issue #61) ------------------------------------------------


def test_configure_logging_telemetry_disabled_attaches_no_handler_or_directory(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: `[telemetry] enabled = false` (the default) is byte-for-byte a no-op —
    no handler attached, no spool directory created.
    """
    spool_dir = tmp_cwd / "spool"
    monkeypatch.setenv("ANSINA_TELEMETRY__SPOOL_DIR", str(spool_dir))

    configure_logging(load_settings())

    root = logging.getLogger()
    handler_names = {h.get_name() for h in root.handlers}
    assert handler_names == {"ansina.json"}
    assert not spool_dir.exists()


def test_configure_logging_telemetry_enabled_attaches_a_second_handler(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_TELEMETRY__ENABLED", "true")
    monkeypatch.setenv("ANSINA_TELEMETRY__SPOOL_DIR", str(tmp_cwd / "spool"))

    configure_logging(load_settings())

    root = logging.getLogger()
    handler_names = {h.get_name() for h in root.handlers}
    assert handler_names == {"ansina.json", "ansina.telemetry.log_mirror"}


def test_configure_logging_telemetry_mirror_redacts_identically_to_primary(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool_dir = tmp_cwd / "spool"
    monkeypatch.setenv("ANSINA_TELEMETRY__ENABLED", "true")
    monkeypatch.setenv("ANSINA_TELEMETRY__SPOOL_DIR", str(spool_dir))
    # Issue #28: `api_token` requires `admin_username` alongside it.
    monkeypatch.setenv("ANSINA_SECURITY__ADMIN_USERNAME", "configured-admin")
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", "mirror-test-secret-token-value12")
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    try:
        configure_logging(load_settings())
        log = get_logger("ansina.tests.setup")
        log.info("token in use: mirror-test-secret-token-value12")

        mirrored = (spool_dir / "log.jsonl").read_text()
        assert "mirror-test-secret-token-value12" not in stream.getvalue()
        assert "mirror-test-secret-token-value12" not in mirrored
        primary_message = json.loads(stream.getvalue().splitlines()[0])["message"]
        mirrored_message = json.loads(mirrored.splitlines()[0])["message"]
        assert primary_message == mirrored_message
    finally:
        clear_secrets()


def test_configure_logging_telemetry_mirror_passes_through_the_three_budget_fields(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tiny `max_file_bytes` proves the handler's own construction kwargs (not
    just its existence) came from `[telemetry]` settings — the mirror rotates on
    the very first write past that threshold.
    """
    spool_dir = tmp_cwd / "spool"
    monkeypatch.setenv("ANSINA_TELEMETRY__ENABLED", "true")
    monkeypatch.setenv("ANSINA_TELEMETRY__SPOOL_DIR", str(spool_dir))
    monkeypatch.setenv("ANSINA_TELEMETRY__MAX_FILE_BYTES", "1")
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    configure_logging(load_settings())
    log = get_logger("ansina.tests.setup")
    log.info("first")
    log.info("second")

    assert list(spool_dir.glob("log-*.jsonl"))
