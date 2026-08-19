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
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", "configured-secret-token-value")
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    try:
        configure_logging(load_settings())
        log = get_logger("ansina.tests.setup")
        log.info("token in use: configured-secret-token-value")

        assert "configured-secret-token-value" not in stream.getvalue()
    finally:
        clear_secrets()
