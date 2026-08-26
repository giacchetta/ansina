from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from ansina import __main__
from ansina.config import ConfigError, Settings, load_settings
from ansina.heart.runtime import HeartUnavailableError


def test_main_boots_uvicorn_with_the_loaded_settings(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: `main()` loads settings, configures logging, builds the app, and
    hands it to `uvicorn.run` with the settings' host/port — in that order, so a
    config failure never reaches uvicorn and logging is JSON before the first log line.
    """
    settings = load_settings()
    sentinel_app = FastAPI()
    calls: list[str] = []

    def _fake_load_settings() -> Settings:
        calls.append("load_settings")
        return settings

    def _fake_configure_logging(_: Settings) -> None:
        calls.append("configure_logging")

    def _fake_create_app(_: Settings) -> FastAPI:
        calls.append("create_app")
        return sentinel_app

    run_kwargs: dict[str, Any] = {}

    def _fake_uvicorn_run(app: FastAPI, **kwargs: Any) -> None:
        calls.append("uvicorn.run")
        run_kwargs["app"] = app
        run_kwargs.update(kwargs)

    monkeypatch.setattr(__main__, "load_settings", _fake_load_settings)
    monkeypatch.setattr(__main__, "configure_logging", _fake_configure_logging)
    monkeypatch.setattr(__main__, "create_app", _fake_create_app)
    # String target (not `__main__.uvicorn.run`): `uvicorn` is a plain module-level
    # import in `__main__.py`, not a re-exported attribute, so mypy's typed-package
    # check rejects reaching it as `__main__.uvicorn`.
    monkeypatch.setattr("ansina.__main__.uvicorn.run", _fake_uvicorn_run)

    __main__.main()

    assert calls == ["load_settings", "configure_logging", "create_app", "uvicorn.run"]
    assert run_kwargs["app"] is sentinel_app
    assert run_kwargs["host"] == settings.server.host
    assert run_kwargs["port"] == settings.server.port
    assert run_kwargs["log_config"] is None


def test_main_exits_non_zero_and_prints_to_stderr_on_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No logger exists yet when config loading fails (logging isn't configured until
    settings load successfully), so the failure must go straight to stderr, not a
    traceback, with a clean non-zero exit.
    """

    def _raise() -> Settings:
        raise ConfigError("bad config")

    monkeypatch.setattr(__main__, "load_settings", _raise)

    with pytest.raises(SystemExit) as exc_info:
        __main__.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "bad config" in captured.err
    assert captured.out == ""


def test_main_exits_non_zero_and_prints_to_stderr_when_create_app_raises(
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`create_app` can now fail before uvicorn ever binds (issue #10's Heart
    capability probe) — same clean stderr-and-exit-1 shape as a `ConfigError`, never
    a traceback, and uvicorn must never be reached.
    """

    def _raise(_settings: Settings) -> None:
        raise HeartUnavailableError("no viable heart runtime on this host")

    monkeypatch.setattr(__main__, "create_app", _raise)
    run_calls: list[str] = []
    monkeypatch.setattr(
        "ansina.__main__.uvicorn.run", lambda *a, **k: run_calls.append("run")
    )

    with pytest.raises(SystemExit) as exc_info:
        __main__.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "no viable heart runtime" in captured.err
    assert captured.out == ""
    assert run_calls == []
