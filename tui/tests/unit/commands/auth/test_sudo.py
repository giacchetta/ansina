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

import ansina_tui.commands.auth.sudo as auth_sudo_module
from ansina_tui.config import HostEntry, load_hosts, save_hosts
from ansina_tui.exits import ExitCode
from ansina_tui.main import app

runner = CliRunner()


def _must_not_be_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request: {request.method} {request.url}")


def test_revoke_and_status_together_is_a_usage_error(tmp_xdg_home: Path) -> None:
    result = runner.invoke(
        app, ["--host", "http://x", "auth", "sudo", "--revoke", "--status"]
    )

    assert result.exit_code == ExitCode.USAGE


def test_status_with_no_grant_never_touches_the_server(
    tmp_xdg_home: Path, patch_transport: Callable[[httpx.BaseTransport], None]
) -> None:
    patch_transport(httpx.MockTransport(_must_not_be_called))

    result = runner.invoke(app, ["--host", "http://x", "auth", "sudo", "--status"])

    assert result.exit_code == ExitCode.OK
    assert "none" in result.stdout


def test_status_with_active_grant_reports_active_without_touching_server(
    tmp_xdg_home: Path,
    patch_transport: Callable[[httpx.BaseTransport], None],
    iso: Callable[[timedelta], str],
    frozen_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_sudo_module, "_now", lambda: frozen_now)
    save_hosts(
        {
            "http://x": HostEntry(
                token="t", sudo_token="g", sudo_expires_at=iso(timedelta(minutes=10))
            )
        }
    )
    patch_transport(httpx.MockTransport(_must_not_be_called))

    result = runner.invoke(app, ["--host", "http://x", "auth", "sudo", "--status"])

    assert result.exit_code == ExitCode.OK
    assert "active" in result.stdout


def test_status_with_expired_grant_self_heals_and_reports_none(
    tmp_xdg_home: Path,
    patch_transport: Callable[[httpx.BaseTransport], None],
    iso: Callable[[timedelta], str],
    frozen_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_sudo_module, "_now", lambda: frozen_now)
    save_hosts(
        {
            "http://x": HostEntry(
                token="t", sudo_token="g", sudo_expires_at=iso(timedelta(minutes=-5))
            )
        }
    )
    patch_transport(httpx.MockTransport(_must_not_be_called))

    result = runner.invoke(app, ["--host", "http://x", "auth", "sudo", "--status"])

    assert result.exit_code == ExitCode.OK
    assert "expired" in result.stdout
    assert load_hosts()["http://x"].sudo_token is None


def test_status_json_mode(
    tmp_xdg_home: Path, patch_transport: Callable[[httpx.BaseTransport], None]
) -> None:
    patch_transport(httpx.MockTransport(_must_not_be_called))

    result = runner.invoke(
        app, ["--host", "http://x", "--json", "auth", "sudo", "--status"]
    )

    assert json.loads(result.stdout) == {"sudo": "none", "expires_at": None}


def test_step_up_with_no_credential_exits_3(tmp_xdg_home: Path) -> None:
    result = runner.invoke(app, ["--host", "http://x", "auth", "sudo"], input="pw\n")

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED


def test_successful_step_up_stores_the_grant(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/sudo"): json_response(
                    200,
                    {
                        "token": "grant-abc",
                        "expires_at": "2026-01-01T13:00:00+00:00",
                        "verifier": "password",
                    },
                )
            }
        )
    )

    result = runner.invoke(
        app, ["--host", "http://x", "auth", "sudo"], input="correct-password\n"
    )

    assert result.exit_code == ExitCode.OK
    entry = load_hosts()["http://x"]
    assert entry.sudo_token == "grant-abc"
    assert entry.sudo_expires_at == "2026-01-01T13:00:00+00:00"


def test_step_up_json_mode(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/sudo"): json_response(
                    200,
                    {
                        "token": "grant-abc",
                        "expires_at": "2026-01-01T13:00:00+00:00",
                        "verifier": "password",
                    },
                )
            }
        )
    )

    result = runner.invoke(
        app, ["--host", "http://x", "--json", "auth", "sudo"], input="pw\n"
    )

    payload = json.loads(result.stdout)
    assert payload == {"sudo": "active", "expires_at": "2026-01-01T13:00:00+00:00"}


def test_wrong_password_reports_verification_failed_not_the_login_hint(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/sudo"): json_response(
                    401,
                    {
                        "type": "urn:ansina:error:ansina.unauthorized",
                        "title": "Unauthorized",
                        "status": 401,
                        "detail": "step-up verification failed",
                        "code": "ansina.unauthorized",
                    },
                )
            }
        )
    )

    result = runner.invoke(
        app, ["--host", "http://x", "auth", "sudo"], input="wrong-password\n"
    )

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED
    assert "Password verification failed" in result.output
    assert "Run `auth login`" not in result.output


def test_lockout_renders_retry_after_and_exits_1(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/sudo"): json_response(
                    429,
                    {
                        "type": "urn:ansina:error:ansina.auth.sudo_locked_out",
                        "title": "Locked Out",
                        "status": 429,
                        "detail": "too many attempts",
                        "code": "ansina.auth.sudo_locked_out",
                        "retry_after_seconds": 17,
                    },
                    headers={"Retry-After": "17"},
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "sudo"], input="pw\n")

    assert result.exit_code == ExitCode.REQUEST_FAILED
    assert "17" in result.output


def test_password_never_appears_in_output_even_when_verbose(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/sudo"): json_response(
                    200,
                    {
                        "token": "grant-abc",
                        "expires_at": "2026-01-01T13:00:00+00:00",
                        "verifier": "password",
                    },
                )
            }
        )
    )
    secret_password = "correct horse battery staple"

    result = runner.invoke(
        app,
        ["--host", "http://x", "--verbose", "auth", "sudo"],
        input=f"{secret_password}\n",
    )

    assert secret_password not in result.output


def test_revoke_clears_the_local_grant(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
    iso: Callable[[timedelta], str],
) -> None:
    save_hosts(
        {
            "http://x": HostEntry(
                token="t", sudo_token="g", sudo_expires_at=iso(timedelta(minutes=10))
            )
        }
    )
    patch_transport(
        mock_transport({("DELETE", "/auth/sudo"): json_response(204, None)})
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "sudo", "--revoke"])

    assert result.exit_code == ExitCode.OK
    assert load_hosts()["http://x"].sudo_token is None


def test_revoke_json_mode(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport({("DELETE", "/auth/sudo"): json_response(204, None)})
    )

    result = runner.invoke(
        app, ["--host", "http://x", "--json", "auth", "sudo", "--revoke"]
    )

    assert json.loads(result.stdout) == {"sudo": "none"}


def test_revoke_with_no_credential_exits_3(tmp_xdg_home: Path) -> None:
    result = runner.invoke(app, ["--host", "http://x", "auth", "sudo", "--revoke"])

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED


def test_step_up_unreachable_host_exits_5(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(unreachable_transport)

    result = runner.invoke(app, ["--host", "http://x", "auth", "sudo"], input="pw\n")

    assert result.exit_code == ExitCode.HOST_UNREACHABLE


def test_sudo_insecure_hosts_file_exits_2(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

    result = runner.invoke(app, ["--host", "http://x", "auth", "sudo", "--status"])

    assert result.exit_code == ExitCode.USAGE
    assert "chmod 600" in result.output


def test_revoke_failure_is_reported_via_the_generic_table(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("DELETE", "/auth/sudo"): json_response(
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

    result = runner.invoke(app, ["--host", "http://x", "auth", "sudo", "--revoke"])

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED


def test_step_up_empty_password_exits_2(tmp_xdg_home: Path) -> None:
    save_hosts({"http://x": HostEntry(token="t")})

    result = runner.invoke(app, ["--host", "http://x", "auth", "sudo"], input="\n")

    assert result.exit_code == ExitCode.USAGE


def test_revoke_unreachable_host_exits_5(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(unreachable_transport)

    result = runner.invoke(app, ["--host", "http://x", "auth", "sudo", "--revoke"])

    assert result.exit_code == ExitCode.HOST_UNREACHABLE
