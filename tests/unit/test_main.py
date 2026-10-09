from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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

    Bare invocation (`argv=[]`, no `--dev`) must also leave Dev Mode a byte-for-byte
    no-op — nothing extra for `dev_mode` to do around `uvicorn.run` here.
    """
    settings = load_settings()
    sentinel_app = FastAPI()
    calls: list[str] = []
    load_settings_overrides: list[Mapping[str, Any] | None] = []

    def _fake_load_settings(*, overrides: Mapping[str, Any] | None = None) -> Settings:
        calls.append("load_settings")
        load_settings_overrides.append(overrides)
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

    __main__.main(argv=[])

    assert calls == ["load_settings", "configure_logging", "create_app", "uvicorn.run"]
    assert load_settings_overrides == [None]
    assert run_kwargs["app"] is sentinel_app
    assert run_kwargs["host"] == settings.server.host
    assert run_kwargs["port"] == settings.server.port
    assert run_kwargs["log_config"] is None


def test_main_dev_flag_passes_the_dev_enabled_override_to_load_settings(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--dev` is the one seam that reaches `ansina.dev.dev_mode` — proven here by
    asserting the exact `overrides` dict `load_settings` receives, not by booting a
    real Vector sidecar (that's `tests/unit/dev/`'s and `tests/e2e`'s job).
    """
    settings = load_settings()
    load_settings_overrides: list[Mapping[str, Any] | None] = []

    def _fake_load_settings(*, overrides: Mapping[str, Any] | None = None) -> Settings:
        load_settings_overrides.append(overrides)
        return settings

    monkeypatch.setattr(__main__, "load_settings", _fake_load_settings)
    monkeypatch.setattr(__main__, "configure_logging", lambda _settings: None)
    monkeypatch.setattr(__main__, "create_app", lambda _settings: FastAPI())
    monkeypatch.setattr("ansina.__main__.uvicorn.run", lambda *a, **k: None)

    __main__.main(argv=["--dev"])

    assert load_settings_overrides == [{"dev": {"enabled": True}}]


def test_main_wraps_uvicorn_run_in_dev_mode(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dev_mode(settings)` must wrap the `uvicorn.run` call — entered before it and
    exited after, so a spawned sidecar is stopped once the server actually returns
    (on a clean SIGTERM shutdown), not before.
    """
    settings = load_settings()
    calls: list[str] = []

    monkeypatch.setattr(__main__, "load_settings", lambda **_k: settings)
    monkeypatch.setattr(__main__, "configure_logging", lambda _settings: None)
    monkeypatch.setattr(__main__, "create_app", lambda _settings: FastAPI())
    monkeypatch.setattr(
        "ansina.__main__.uvicorn.run", lambda *a, **k: calls.append("uvicorn.run")
    )

    @contextmanager
    def _fake_dev_mode(_settings: Settings) -> Iterator[None]:
        calls.append("dev_mode enter")
        yield
        calls.append("dev_mode exit")

    monkeypatch.setattr(__main__, "dev_mode", _fake_dev_mode)

    __main__.main(argv=[])

    assert calls == ["dev_mode enter", "uvicorn.run", "dev_mode exit"]


def test_main_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        __main__.main(argv=["--help"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--dev" in captured.out


def test_main_unrecognized_argument_exits_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        __main__.main(argv=["--not-a-real-flag"])

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""


def test_main_exits_non_zero_and_prints_to_stderr_on_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No logger exists yet when config loading fails (logging isn't configured until
    settings load successfully), so the failure must go straight to stderr, not a
    traceback, with a clean non-zero exit.
    """

    def _raise(*, overrides: Mapping[str, Any] | None = None) -> Settings:
        raise ConfigError("bad config")

    monkeypatch.setattr(__main__, "load_settings", _raise)

    with pytest.raises(SystemExit) as exc_info:
        __main__.main(argv=[])

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
        __main__.main(argv=[])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "no viable heart runtime" in captured.err
    assert captured.out == ""
    assert run_calls == []
