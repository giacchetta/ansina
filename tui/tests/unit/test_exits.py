"""Every member of `ExitCode` produced by at least one scenario here — self-contained
(no dependency on other test modules or their run order) so this stays a reliable
pin even as commands are added in #32/#33.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from ansina_tui.exits import ExitCode
from ansina_tui.main import app
from ansina_tui.problems import exit_code_for

runner = CliRunner()


def _healthy_ready_routes(
    json_response: Callable[..., httpx.Response],
) -> dict[str, httpx.Response]:
    return {
        "/healthz": json_response(200, {"status": "ok"}),
        "/readyz": json_response(
            200, {"status": "ready", "checks": {"database": True}}
        ),
        "/version": json_response(200, {"name": "ansina", "version": "0.1.0"}),
    }


def test_exit_code_values_match_the_documented_table() -> None:
    assert ExitCode.OK.value == 0
    assert ExitCode.REQUEST_FAILED.value == 1
    assert ExitCode.USAGE.value == 2
    assert ExitCode.NOT_AUTHENTICATED.value == 3
    assert ExitCode.FORBIDDEN.value == 4
    assert ExitCode.HOST_UNREACHABLE.value == 5
    assert ExitCode.NOT_READY.value == 6
    assert ExitCode.NOT_HEALTHY.value == 7


def test_ok_produced_by_status_against_a_healthy_ready_daemon(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_status_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_status_transport(mock_transport(_healthy_ready_routes(json_response)))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == ExitCode.OK


def test_request_failed_produced_by_problems_exit_code_for() -> None:
    assert exit_code_for(500) == ExitCode.REQUEST_FAILED


def test_usage_produced_by_an_insecure_hosts_file(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)  # 0640

    result = runner.invoke(app, ["status"])
    assert result.exit_code == ExitCode.USAGE


def test_not_authenticated_produced_by_problems_exit_code_for() -> None:
    assert exit_code_for(401) == ExitCode.NOT_AUTHENTICATED


def test_forbidden_produced_by_problems_exit_code_for() -> None:
    assert exit_code_for(403) == ExitCode.FORBIDDEN


def test_host_unreachable_produced_by_status_against_a_dead_transport(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_status_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_status_transport(unreachable_transport)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == ExitCode.HOST_UNREACHABLE


def test_not_ready_produced_by_status_against_a_ready_false_daemon(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_status_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    routes = _healthy_ready_routes(json_response)
    routes["/readyz"] = json_response(
        503,
        {
            "type": "urn:ansina:error:ansina.not_ready",
            "title": "Not Ready",
            "status": 503,
            "detail": "One or more readiness checks are failing.",
            "code": "ansina.not_ready",
            "checks": {"database": False},
        },
    )
    patch_status_transport(mock_transport(routes))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == ExitCode.NOT_READY


def test_not_healthy_produced_by_status_against_a_failing_healthz(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_status_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    routes = _healthy_ready_routes(json_response)
    routes["/healthz"] = json_response(500, {"detail": "boom"})
    patch_status_transport(mock_transport(routes))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == ExitCode.NOT_HEALTHY


def test_every_exit_code_has_a_dedicated_scenario_above() -> None:
    """Fails loudly the moment `ExitCode` grows a member with no test above producing
    it — cross-checked against the literal set of scenarios this module implements."""
    exercised = {
        ExitCode.OK,
        ExitCode.REQUEST_FAILED,
        ExitCode.USAGE,
        ExitCode.NOT_AUTHENTICATED,
        ExitCode.FORBIDDEN,
        ExitCode.HOST_UNREACHABLE,
        ExitCode.NOT_READY,
        ExitCode.NOT_HEALTHY,
    }
    assert exercised == set(ExitCode)
