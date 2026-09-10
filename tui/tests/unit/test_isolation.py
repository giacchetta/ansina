"""Pins the milestone's load-bearing constraint: `tui/` never imports `ansina` — it
talks HTTP only, exactly like any third-party client (see `README.md`)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from ansina_tui.main import app

runner = CliRunner()


def _no_ansina_daemon_module_imported() -> bool:
    return not any(
        name == "ansina" or name.startswith("ansina.") for name in sys.modules
    )


def test_ansina_daemon_package_never_imported_by_help(tmp_xdg_home: Path) -> None:
    runner.invoke(app, ["--help"])
    assert _no_ansina_daemon_module_imported()


def test_ansina_daemon_package_never_imported_by_status(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
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

    runner.invoke(app, ["--json", "status"])

    assert _no_ansina_daemon_module_imported()


def test_ansina_daemon_package_never_imported_by_an_auth_run(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(
        mock_transport(
            {
                "/auth/me": json_response(
                    200,
                    {
                        "username": "alice",
                        "roles": ["read"],
                        "auth_method": "api_token",
                    },
                )
            }
        )
    )

    runner.invoke(app, ["--json", "auth", "login", "--with-token"], input="tok\n")

    assert _no_ansina_daemon_module_imported()


def test_ansina_daemon_package_never_imported_by_an_api_run(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(
        mock_transport({"/version": json_response(200, {"name": "ansina"})})
    )

    runner.invoke(app, ["--json", "api", "/version"])

    assert _no_ansina_daemon_module_imported()
