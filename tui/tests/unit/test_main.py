"""Pins the no-args rule: bare invocation on a TTY opens the TUI; any argument at all
— a subcommand, `--help`, `--version` — is the CLI and never touches `launch_tui`;
bare on a non-TTY writes help to stderr and exits 2.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

import ansina_tui.main as main_module
from ansina_tui.context import AppContext
from ansina_tui.exits import ExitCode
from ansina_tui.main import app

runner = CliRunner()


@pytest.fixture
def fake_launch(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Records `launch_tui` calls instead of actually starting a Textual app."""
    calls: list[object] = []
    monkeypatch.setattr(
        main_module, "launch_tui", lambda app_context: calls.append(app_context)
    )
    return calls


def test_bare_interactive_launches_the_tui(
    fake_launch: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module, "_is_interactive", lambda: True)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert len(fake_launch) == 1


def test_bare_non_interactive_writes_help_to_stderr_and_exits_2(
    fake_launch: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module, "_is_interactive", lambda: False)

    result = runner.invoke(app, [])

    assert result.exit_code == ExitCode.USAGE
    assert fake_launch == []
    assert result.stdout == ""
    assert "Usage" in result.stderr


def test_help_writes_to_stdout_and_never_launches(fake_launch: list[object]) -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Usage" in result.stdout
    assert fake_launch == []


def test_status_help_writes_to_stdout_and_never_launches(
    fake_launch: list[object],
) -> None:
    result = runner.invoke(app, ["status", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.stdout
    assert fake_launch == []


def test_version_writes_to_stdout_and_never_launches(fake_launch: list[object]) -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "ansina-tui" in result.stdout
    assert fake_launch == []


def test_subcommand_dispatch_skips_the_tui_branch_even_when_interactive(
    tmp_xdg_home: Path,
    fake_launch: list[object],
    monkeypatch: pytest.MonkeyPatch,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    monkeypatch.setattr(main_module, "_is_interactive", lambda: True)
    patch_transport(
        mock_transport(
            {
                "/healthz": json_response(200, {"status": "ok"}),
                "/readyz": json_response(200, {"status": "ready", "checks": {}}),
                "/version": json_response(200, {"name": "ansina", "version": "0.1.0"}),
            }
        )
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == ExitCode.OK
    assert fake_launch == []


def test_host_flag_threads_through_to_the_command_layer(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(
        mock_transport(
            {
                "/healthz": json_response(200, {"status": "ok"}),
                "/readyz": json_response(200, {"status": "ready", "checks": {}}),
                "/version": json_response(200, {"name": "ansina", "version": "0.1.0"}),
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://custom:9999", "--json", "status"])

    assert result.exit_code == ExitCode.OK
    assert '"host": "http://custom:9999"' in result.stdout


def test_bare_auth_is_a_usage_error_not_silent_success(
    fake_launch: list[object],
) -> None:
    result = runner.invoke(app, ["auth"])

    assert result.exit_code == ExitCode.USAGE
    assert fake_launch == []
    assert "Usage" in result.stderr


def test_bare_auth_token_is_a_usage_error(fake_launch: list[object]) -> None:
    result = runner.invoke(app, ["auth", "token"])

    assert result.exit_code == ExitCode.USAGE
    assert "Usage" in result.stderr


def test_auth_help_writes_to_stdout_and_never_launches(
    fake_launch: list[object],
) -> None:
    result = runner.invoke(app, ["auth", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.stdout
    assert fake_launch == []


def test_is_interactive_reflects_the_real_tty_state() -> None:
    """Exercises the real `sys.stdin.isatty()`/`sys.stdout.isatty()` check (every
    other test above patches `_is_interactive` itself, per its own docstring)."""
    assert main_module._is_interactive() == (sys.stdin.isatty() and sys.stdout.isatty())


def test_launch_tui_builds_the_app_with_the_whole_context_and_runs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class _FakeApp:
        def __init__(self, app_context: AppContext) -> None:
            calls.append(("init", app_context))

        def run(self) -> None:
            calls.append(("run",))

    monkeypatch.setattr("ansina_tui.ui.app.AnsinaTuiApp", _FakeApp)
    context = AppContext(host="http://x", json_output=False, verbose=False)

    main_module.launch_tui(context)

    assert calls == [("init", context), ("run",)]


def test_refresh_defaults_to_five_seconds(
    tmp_xdg_home: Path,
    fake_launch: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_is_interactive", lambda: True)

    result = runner.invoke(app, [])

    assert result.exit_code == ExitCode.OK
    context = fake_launch[0]
    assert isinstance(context, AppContext)
    assert context.refresh == 5.0


def test_refresh_flag_threads_through_to_the_app_context(
    tmp_xdg_home: Path,
    fake_launch: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_is_interactive", lambda: True)

    result = runner.invoke(app, ["--refresh", "10"])

    assert result.exit_code == ExitCode.OK
    context = fake_launch[0]
    assert isinstance(context, AppContext)
    assert context.refresh == 10.0


def test_refresh_zero_or_negative_is_a_usage_error(
    fake_launch: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_is_interactive", lambda: True)

    result = runner.invoke(app, ["--refresh", "0"])

    assert result.exit_code == ExitCode.USAGE
    assert fake_launch == []
    assert "--refresh must be greater than 0" in result.stderr


def test_refresh_is_ignored_by_subcommands(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(
        mock_transport(
            {
                "/healthz": json_response(200, {"status": "ok"}),
                "/readyz": json_response(200, {"status": "ready", "checks": {}}),
                "/version": json_response(200, {"name": "ansina", "version": "0.1.0"}),
            }
        )
    )

    result = runner.invoke(app, ["--refresh", "0", "status"])

    assert result.exit_code == ExitCode.OK
