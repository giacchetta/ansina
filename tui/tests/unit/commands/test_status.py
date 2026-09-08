from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from ansina_tui.exits import ExitCode
from ansina_tui.main import app

runner = CliRunner()


def _routes(
    json_response: Callable[..., httpx.Response],
    *,
    healthy: bool = True,
    ready: bool = True,
    version_status: int = 200,
) -> dict[str, httpx.Response]:
    routes = {
        "/healthz": json_response(
            200 if healthy else 500, {"status": "ok" if healthy else "down"}
        ),
        "/version": json_response(
            version_status,
            {"name": "ansina", "version": "0.1.0"}
            if version_status == 200
            else {"code": "ansina.unauthorized"},
        ),
    }
    if ready:
        routes["/readyz"] = json_response(
            200, {"status": "ready", "checks": {"database": True}}
        )
    else:
        routes["/readyz"] = json_response(
            503,
            {
                "type": "urn:ansina:error:ansina.not_ready",
                "title": "Not Ready",
                "status": 503,
                "detail": "One or more readiness checks are failing.",
                "code": "ansina.not_ready",
                "checks": {"database": False, "heart": True},
            },
        )
    return routes


def test_status_renders_with_no_token(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_status_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_status_transport(mock_transport(_routes(json_response)))

    result = runner.invoke(app, ["status"])

    assert result.exit_code == ExitCode.OK
    assert "ready" in result.stdout


def test_status_ready_with_no_checks_key_renders_with_an_empty_check_list(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_status_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    routes = _routes(json_response)
    routes["/readyz"] = json_response(200, {"status": "ready"})
    patch_status_transport(mock_transport(routes))

    result = runner.invoke(app, ["status"])

    assert result.exit_code == ExitCode.OK
    assert "ready" in result.stdout


def test_status_renders_with_a_token(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_status_transport: Callable[[httpx.BaseTransport], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANSINA_TOKEN", "test-token")
    patch_status_transport(mock_transport(_routes(json_response)))

    result = runner.invoke(app, ["status"])

    assert result.exit_code == ExitCode.OK


def test_status_version_401_degrades_to_unknown(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_status_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_status_transport(mock_transport(_routes(json_response, version_status=401)))

    result = runner.invoke(app, ["status"])

    assert result.exit_code == ExitCode.OK
    assert "unknown" in result.stdout


def test_status_json_emits_parseable_json_on_stdout_only(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_status_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_status_transport(mock_transport(_routes(json_response)))

    result = runner.invoke(app, ["--json", "status"])

    assert result.exit_code == ExitCode.OK
    payload = json.loads(result.stdout)
    assert payload["healthy"] is True
    assert payload["ready"] is True
    assert payload["checks"] == {"database": True}
    assert payload["version"] == "ansina 0.1.0"


def test_status_not_ready_exits_6_and_lists_failing_check(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_status_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_status_transport(mock_transport(_routes(json_response, ready=False)))

    result = runner.invoke(app, ["status"])

    assert result.exit_code == ExitCode.NOT_READY
    assert "NOT READY" in result.stdout
    assert "database" in result.stdout


def test_status_not_healthy_exits_7_even_when_ready_would_be_true(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_status_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_status_transport(mock_transport(_routes(json_response, healthy=False)))

    result = runner.invoke(app, ["status"])

    assert result.exit_code == ExitCode.NOT_HEALTHY


def test_status_unreachable_host_exits_5_with_a_clear_message_not_a_traceback(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_status_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_status_transport(unreachable_transport)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == ExitCode.HOST_UNREACHABLE
    assert "Traceback" not in result.output
    assert "Could not reach" in result.output


def test_status_insecure_hosts_file_exits_2_with_actionable_message(
    tmp_xdg_home: Path,
) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == ExitCode.USAGE
    assert "chmod 600" in result.output
