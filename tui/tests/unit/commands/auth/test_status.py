from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

import ansina_tui.commands.auth.status as auth_status_module
from ansina_tui.config import HostEntry, save_hosts
from ansina_tui.exits import ExitCode
from ansina_tui.main import app

runner = CliRunner()


def _me_response(
    json_response: Callable[..., httpx.Response],
    *,
    roles: list[str] | None = None,
) -> dict[str | tuple[str, str], httpx.Response]:
    return {
        "/auth/me": json_response(
            200,
            {
                "user_id": "u1",
                "username": "alice",
                "display_name": "Alice",
                "roles": roles or ["read", "maintain"],
                "auth_method": "api_token",
                "sudo_active": False,
            },
        )
    }


def test_status_no_credential_exits_3(tmp_xdg_home: Path) -> None:
    result = runner.invoke(app, ["--host", "http://x", "auth", "status"])

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED
    assert "auth login" in result.output


def test_status_renders_identity_and_sudo_none(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="tok")})
    patch_transport(mock_transport(_me_response(json_response)))

    result = runner.invoke(app, ["--host", "http://x", "auth", "status"])

    assert result.exit_code == ExitCode.OK
    assert "alice" in result.stdout
    assert "read" in result.stdout
    assert "none" in result.stdout


def test_status_renders_active_sudo_grant(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
    iso: Callable[[timedelta], str],
    frozen_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_status_module, "_now", lambda: frozen_now)
    save_hosts(
        {
            "http://x": HostEntry(
                token="tok",
                sudo_token="grant",
                sudo_expires_at=iso(timedelta(minutes=10)),
            )
        }
    )
    patch_transport(mock_transport(_me_response(json_response)))

    result = runner.invoke(app, ["--host", "http://x", "auth", "status"])

    assert result.exit_code == ExitCode.OK
    assert "active" in result.stdout


def test_status_renders_expired_sudo_grant_distinctly(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
    iso: Callable[[timedelta], str],
    frozen_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_status_module, "_now", lambda: frozen_now)
    save_hosts(
        {
            "http://x": HostEntry(
                token="tok",
                sudo_token="grant",
                sudo_expires_at=iso(timedelta(minutes=-10)),
            )
        }
    )
    patch_transport(mock_transport(_me_response(json_response)))

    result = runner.invoke(app, ["--host", "http://x", "auth", "status"])

    assert result.exit_code == ExitCode.OK
    assert "expired" in result.stdout


def test_status_json_mode(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="tok")})
    patch_transport(mock_transport(_me_response(json_response)))

    result = runner.invoke(app, ["--host", "http://x", "--json", "auth", "status"])

    payload = json.loads(result.stdout)
    assert payload["username"] == "alice"
    assert payload["sudo"] == "none"
    assert payload["credential_source"] == "stored"


def test_status_credential_source_reports_env_token(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANSINA_TOKEN", "env-token")
    patch_transport(mock_transport(_me_response(json_response)))

    result = runner.invoke(app, ["--host", "http://x", "--json", "auth", "status"])

    payload = json.loads(result.stdout)
    assert payload["credential_source"] == "ANSINA_TOKEN"


def test_status_rejected_stored_token_exits_3(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="stale")})
    patch_transport(
        mock_transport(
            {
                "/auth/me": json_response(
                    401,
                    {
                        "type": "urn:ansina:error:ansina.unauthorized",
                        "title": "Unauthorized",
                        "status": 401,
                        "detail": "no resolved identity",
                        "code": "ansina.unauthorized",
                    },
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "status"])

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED


def test_status_unreachable_host_exits_5(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="tok")})
    patch_transport(unreachable_transport)

    result = runner.invoke(app, ["--host", "http://x", "auth", "status"])

    assert result.exit_code == ExitCode.HOST_UNREACHABLE


def test_status_insecure_hosts_file_exits_2(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

    result = runner.invoke(app, ["--host", "http://x", "auth", "status"])

    assert result.exit_code == ExitCode.USAGE
    assert "chmod 600" in result.output


def test_no_show_token_flag_exists(tmp_xdg_home: Path) -> None:
    """Pins the deliberate absence of `--show-token` — a token is shown once, at
    mint time, never again."""
    result = runner.invoke(app, ["auth", "status", "--show-token"])

    assert result.exit_code != 0


def test_status_never_prints_the_token(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    secret = "super-secret-token-value"
    save_hosts({"http://x": HostEntry(token=secret)})
    patch_transport(mock_transport(_me_response(json_response)))

    result = runner.invoke(app, ["--host", "http://x", "--verbose", "auth", "status"])

    assert secret not in result.output
